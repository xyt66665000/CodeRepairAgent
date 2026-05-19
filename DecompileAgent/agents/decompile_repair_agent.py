"""
Phase 1: Decompile Repair Agent

Makes decompiled C pseudocode compilable with strict gcc.
Iteratively: compile -> diagnose -> patch -> recompile.

Corresponds to the `decompile-repair` skill.
"""

import os
import re
import datetime
from typing import Dict, Tuple

from openai import OpenAI
from dotenv import load_dotenv

from agents.base_agent import (
    API_KEY,
    BASE_URL,
    MODEL_NAME,
    MAX_CONTEXT_WINDOW,
    DEFAULT_COMPILE_CMD,
    BaseReActAgent,
    AsyncJsonlLogger,
    build_common_tools,
    compile_c_file,
    verify_compilation,
    get_file_metrics,
    save_agent_result,
    cprint,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Gate check — Phase 1 always runs
# ---------------------------------------------------------------------------


def gate_check(filepath: str) -> Tuple[bool, str]:
    """Phase 1 always runs — it is the pipeline entry point."""
    return True, "Phase 1 always runs (pipeline entry point)"


# ---------------------------------------------------------------------------
# Deterministic preprocessor — fixes generic IDA-isms before the ReAct loop
# ---------------------------------------------------------------------------
#
# These are patterns that appear in EVERY IDA Pro / Hex-Rays decompiled .c file,
# regardless of the binary being decompiled.  File-specific type definitions,
# missing struct fields, and extern declarations are left to the LLM agent.
# ---------------------------------------------------------------------------

# Generic calling-convention macros used by IDA in all decompiled output.
# GCC does not recognize them, so they must be defined as empty.
GENERIC_CALLING_CONVENTIONS = """\
// Repaired: generic IDA calling-convention placeholders
#define __cdecl
#define __fastcall
#define __usercall
#define __thiscall
#define __stdcall
"""


def preprocess_c_file(filepath: str) -> None:
    """Apply generic, deterministic fixes to IDA-decompiled C pseudocode.

    Only fixes patterns that are **universal** across all IDA output:
      1. Insert calling-convention macros (__cdecl, __fastcall, etc.)
      2. Replace MSVC ``__asm { ... }`` → GCC inline asm stub
      3. Remove IDA register annotations (``@<eax>``, ``@<edx>``, etc.)
      4. Fix ``_UNKNOWN *var = &var;`` self-referencing weak-symbol pattern

    File-specific issues (missing types, struct fields, extern declarations,
    type mismatches) are intentionally left for the LLM agent to handle.
    """
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    original = content

    # 1. Insert calling-convention macros after the last #include line.
    #    Find the last #include to place the preamble after all system headers.
    last_include = 0
    for m in re.finditer(r'^#include\s+[<\"].*[>\"]\s*$', content, re.MULTILINE):
        last_include = m.end()
    if last_include > 0:
        # Insert after the newline following the last #include
        nl_after = content.find("\n", last_include)
        if nl_after != -1:
            insert_pos = nl_after + 1
            content = content[:insert_pos] + GENERIC_CALLING_CONVENTIONS + "\n" + content[insert_pos:]

    # 2. Replace MSVC-style __asm blocks with GCC inline assembly stubs.
    content = re.sub(
        r'__asm\s*\{[^}]*\}',
        '// Repaired: MSVC asm replaced with GCC inline asm stub\n'
        '  __asm__ __volatile__("");',
        content,
    )

    # 3. Remove IDA register annotations (@<eax>, @<edx>, ...).
    #    These appear in __usercall function signatures, e.g.:
    #      void __usercall start(int a1@<eax>, void (*fn)(void)@<edx>)
    content = re.sub(r'@<\w+>', '', content)

    # 4. Fix "_UNKNOWN *var = &var;" self-referencing weak-symbol pattern.
    #    _UNKNOWN is #defined as char in defs.h, so it expands to
    #    "char *var = &var;" which is a pointer-type mismatch (char * ← char **).
    #    This pattern appears in most IDA outputs for weak extern symbols.
    content = re.sub(
        r'_UNKNOWN \*(\w+) = &\1;',
        r'_UNKNOWN *\1 = (_UNKNOWN *)&\1;  // Repaired: added cast for weak symbol',
        content,
    )

    # Only write if something changed (avoid unnecessary I/O).
    if content != original:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a ReAct-style agent for repairing IDA decompiler C pseudocode so it compiles with strict gcc.

NOTE: The file has already been preprocessed by a deterministic script that:
- Defined generic IDA calling conventions (#define __cdecl, __fastcall, __usercall, __thiscall, __stdcall)
- Replaced MSVC __asm blocks with GCC inline assembly stubs
- Removed IDA register annotations (@<eax>, @<edx>, etc.) from function parameters
- Fixed the "_UNKNOWN *var = &var;" weak-symbol pattern by adding an explicit cast

File-specific issues (missing type definitions, missing struct fields, missing extern
declarations, type mismatches) are NOT fixed by the preprocessor — that is your job.
Start by running `Parse GCC Errors` to see the current compilation errors, then fix
them one category at a time. Do NOT re-add calling-convention macros.

## Supreme Rule: Memory Semantics Above All Syntax Rules
- When an integer/wide-type scalar value (e.g., _QWORD dest[2]) is passed as a pointer argument, cast the VALUE: `(const char *)(dest[2])`.
- NEVER use `&` to fix type mismatches. `&dest[2]` changes "read value from memory" into "read stack address" — this DESTROYS original memory semantics.
- Preserve all overflow/crash trigger paths. Do NOT "fix" buffer overflows or dereferences of attacker-controlled values.

## Rules
- Keep changes minimal. NEVER change program logic.
- NEVER add/remove function parameters.
- NEVER remove or comment out entire functions or logic blocks.
- Add missing `#include` headers (stdlib.h, string.h, etc.) for standard functions.
- Address type errors with VALUE casts: `(TargetType)(scalar_value)`.
- Only use `&`-cast if the original expression ALREADY took an address.
- You MAY add `extern` or global declarations for undefined custom functions.
- Ensure function return boundaries: add `return 0;` or `return NULL;` at end if needed.
- Mark every modification with a C comment: `// Modified: <reason>`.

## Compilation Command
Use: gcc -c -Werror=implicit-function-declaration -Werror=implicit-int -Werror=incompatible-pointer-types -Werror=int-conversion -Werror=return-type -fno-builtin -fmax-errors=0 -I.

## Self-Check Before Every Cast Fix
Before fixing "incompatible pointer types" or "int-conversion":
Q1: Does the argument expression read a VALUE from memory?
    YES — array subscript (arr[i]), pointer dereference (*ptr), struct member access (s.field)
    NO  — array/pointer decay (arr), string literal, function name
If Q1 = YES → use VALUE cast:  (TargetType)(expression)
If Q1 = NO  → use pointer cast: (TargetType)expression

Prefer `Parse GCC Errors` + `Read Code Slice` + `Patch Apply` over dumping whole files.
After each patch, re-run compilation until it succeeds.
"""


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------

TASK_PROMPT = """\
Your task is to ensure that every .c file in the folder compiles successfully with strict gcc.
These files are pseudocode automatically generated by IDA Pro.

The file has been preprocessed to fix generic IDA-isms only:
- Calling-convention macros (__cdecl, __fastcall, __usercall, __thiscall, __stdcall) are already defined.
- MSVC __asm blocks have been replaced with GCC inline asm stubs.
- Register annotations (@<eax>, @<edx>, etc.) have been removed from function parameters.
- "_UNKNOWN *var = &var;" patterns have been fixed with an explicit cast.

File-specific issues you may still need to handle:
- Missing type definitions (struct layouts, typedefs for domain types, etc.)
- Missing #include headers (stdlib.h, string.h, stdint.h, etc.)
- Missing extern declarations for called functions
- Type mismatches (pointer vs integer, incompatible pointer types)
- Implicit function declarations
Start by running Parse GCC Errors to see what remains, then fix errors systematically.

### Compilation Check:
- Use the exact command: `gcc -c -Werror=implicit-function-declaration -Werror=implicit-int -Werror=incompatible-pointer-types -Werror=int-conversion -Werror=return-type -fno-builtin -fmax-errors=0 -I.`
- A successful result means a `.o` file is generated with ZERO errors.
- Do NOT add any redundant suffixes or prefixes to the output `.o` file name.

### File Modification Instructions:
- If there are compilation errors, modify the .c file minimally to fix the errors.
- After each repair, recompile to verify.
- Repeat until ALL .c files compile with zero errors.

### Constraints:
- **Do not change the logic of the original code.**
- You CANNOT add or remove function parameters.
- You CANNOT remove or comment out any function or logic.
- You MAY adjust parameter data types or add `extern`/global declarations if necessary.
- Do not affect the semantics of the original code, including its original control flow, data flow, or logic.

### Memory Semantics (CRITICAL):
- When an expression reads a VALUE from memory (array subscript, dereference), cast the VALUE, never take its address with `&`.
- `printLine((const char *)(dest[2]));` — CORRECT (casts the value read from dest[2]).
- `printLine((const char *)&dest[2]);` — WRONG (takes stack address, destroys memory semantics).
- Preserve vulnerability trigger paths: buffer overflows, attacker-controlled pointer dereferences, etc.

### Commenting Requirement:
- Use comments to clearly mark every modification: `// Modified: <reason>`.
"""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_decompile_repair(
    base_dir: str,
    compile_cmd: str = DEFAULT_COMPILE_CMD,
    max_iterations: int = 1000,
    verbose: bool = True,
) -> Tuple[bool, str]:
    """Run Phase 1: decompile repair.

    Returns:
        (success: bool, message: str)
    """
    # ── Deterministic preprocessing ──
    for fname in os.listdir(base_dir):
        if fname.lower().endswith(".c"):
            c_file_path = os.path.join(base_dir, fname)
            cprint(f"  -> Preprocessing: {fname}", color="blue")
            preprocess_c_file(c_file_path)

    steps_log_path = os.path.join(
        base_dir,
        f"phase1_decompile_repair_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
    )
    step_logger = AsyncJsonlLogger(steps_log_path)

    try:
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        tools = build_common_tools(base_dir, compile_cmd)

        agent = BaseReActAgent(
            client=client,
            tools=tools,
            base_dir=base_dir,
            system_prompt=SYSTEM_PROMPT,
            compile_cmd=compile_cmd,
            max_iterations=max_iterations,
            max_context_tokens=MAX_CONTEXT_WINDOW,
            step_logger=step_logger,
            verbose=verbose,
        )

        result = agent.run(TASK_PROMPT)

        summary = {
            "log_path": steps_log_path,
            "status": result.get("status", "failure"),
            "message": result.get("output", ""),
        }
        save_agent_result(base_dir, summary, agent_name="decompile_repair")

        if result.get("status") == "success":
            return True, result.get("output", "Compilation succeeded.")
        return False, result.get("output", "Unknown error.")

    except Exception as e:
        summary = {
            "log_path": steps_log_path,
            "status": "failure",
            "message": str(e),
        }
        save_agent_result(base_dir, summary, agent_name="decompile_repair")
        return False, str(e)
    finally:
        step_logger.close()
