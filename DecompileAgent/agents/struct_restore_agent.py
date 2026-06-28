"""
Phase 2: Struct & Type Restoration Agent

Restores degraded struct/class types and eliminates raw pointer-arithmetic
member access (PAMA) in decompiled C code.

Corresponds to the `restore-decompiled-structs` skill.
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
    verify_compilation,
    save_agent_result,
    cprint,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Gate check — PAMA or typed degradation patterns
# ---------------------------------------------------------------------------

# Patterns from the restore-decompiled-structs skill
_PAMA_PATTERNS = [
    # IDA custom types
    r'\*\(\((_QWORD|_DWORD|_WORD|_BYTE)\s*\*\s*\)\s*\w+\s*\+',
    r'\*\(\((_QWORD|_DWORD|_WORD|_BYTE)\s*\*\s*\)\s*\(\s*\w+\s*\+',
    # Standard C integer types (8-byte)
    r'\*\(\((long\s+long|unsigned\s+long\s+long|int64_t|uint64_t)\s*\*\s*\)\s*\w+\s*\+',
    # Standard C integer types (4-byte)
    r'\*\(\((int|unsigned\s+int|long|unsigned\s+long|int32_t|uint32_t)\s*\*\s*\)\s*\w+\s*\+',
    # Standard C integer types (2-byte)
    r'\*\(\((short|unsigned\s+short|int16_t|uint16_t)\s*\*\s*\)\s*\w+\s*\+',
    # Standard C integer types (1-byte)
    r'\*\(\((char|unsigned\s+char|int8_t|uint8_t)\s*\*\s*\)\s*\w+\s*\+',
    # Pointer-to-pointer PAMA
    r'\*\(\((void|char|int)\s*\*\*\s*\)\s*\w+\s*\+',
    # Offset-based access: *((_DWORD *)(base + offset))
    r'\*\(\((_DWORD|_QWORD|_WORD|_BYTE|int|unsigned\s+int|long\s+long)\s*\*\)\s*\(',
    # Generic pointer degradation: void * used with PAMA
    r'void\s*\*\s*\w+.*\*\(\((_QWORD|_DWORD|int)\s*\*\s*\)\s*\w+\s*\+',
    # _UNKNOWN * type
    r'_UNKNOWN\s*\*',
]


def gate_check(filepath: str) -> Tuple[bool, str]:
    """Check for PAMA patterns or typed degradation in the file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return False, f"Cannot read file: {filepath}"

    matched_patterns = []
    for pattern in _PAMA_PATTERNS:
        if re.search(pattern, content):
            matched_patterns.append(pattern)

    if matched_patterns:
        return True, f"PAMA/degradation patterns detected: {len(matched_patterns)} pattern(s)"
    return False, "No PAMA or typed degradation patterns detected"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a ReAct-style agent for restoring degraded struct/class types in decompiled C pseudocode.
Your job is to eliminate pointer-arithmetic member access (PAMA) and recover proper struct definitions.

## Supreme Rule: Memory Semantics Above All Syntax Rules
- Preserve original memory read/write paths, dereference levels, and data-flow edges.
- Do NOT add `&` to "fix" type mismatches.
- Do NOT change pointer dereference depth.
- Do NOT alter physical stack or heap access patterns.

## Fundamental Principle: Data Determines Logic (数据决定逻辑)
- This is the FOUNDATION phase. All downstream phases depend on correct types from this phase.
- A hallucinated struct here POISONS everything downstream.

## Anti-Hallucination Rules
1. Every struct field MUST be backed by a concrete PAMA pattern in the code.
   If you cannot point to the exact `*((T *)ptr + N)` expression, do NOT create that field.
2. Every type inference MUST cite evidence — which usage site proves the type.
3. Preserve original when uncertain. If confidence is below MEDIUM, keep the original PAMA.
4. Check compilation after EVERY struct definition. Define one struct, verify, then proceed.

## Pointer-Arithmetic Byte-Offset Law (NON-NEGOTIABLE)
In C, `((T *)ptr + N)` advances by `N * sizeof(T)` bytes, NOT N bytes.

| Expression | sizeof(T) | Byte Offset |
|---|---|---|
| *((_QWORD *)ptr + 2) | 8 | 16 (0x10) |
| *((_DWORD *)ptr + 3) | 4 | 12 (0x0C) |
| *((_WORD *)ptr + 1) | 2 | 2 (0x02) |
| *((_BYTE *)ptr + 7) | 1 | 7 (0x07) |
| *((char **)ptr + 2) | 8 (pointer!) | 16 (0x10) |
| *((void **)ptr + 1) | 8 (pointer!) | 8 (0x08) |

ALWAYS compute byte_offset = N * sizeof(T) before determining which struct member lives at that offset.

## Type Inference Heuristics
| # | Context Clue | Inferred Type | Confidence |
|---|---|---|---|
| 1 | Passed to free() | void * | HIGH |
| 2 | Assigned from malloc()/calloc() | void * | HIGH |
| 3 | Compared against NULL and used as address | pointer type | HIGH |
| 4 | Passed to strcpy/strlen/strcmp | char * | HIGH |
| 5 | Used in integer arithmetic | int / unsigned int | HIGH |
| 6 | Loop counter / array index | int or unsigned int | HIGH |

## Retry Limit
If a struct causes compilation errors that cannot be fixed in 3 attempts, REVERT that struct.
Add a comment: `// Skipped: struct restoration failed — insufficient evidence`.

## Rules
- Replace pointer arithmetic with named member access: `dest->message` instead of `*((_QWORD *)dest + 2)`.
- Define struct types with `typedef struct { ... } name_t;`.
- Add `// Restored:` comments explaining each struct/type recovery decision.
- NEVER change program logic, control flow, or data flow.
- NEVER remove or comment out code blocks.
- Prefer `Read Code Slice` + `Patch Apply` for targeted changes.
- After every struct definition, recompile to verify.
"""


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------

TASK_PROMPT = """\
Your task is to scan the .c file(s) in the folder for pointer-arithmetic member access (PAMA) patterns and restore proper struct definitions.

### Steps:
1. SCAN the file for PAMA patterns:
   - `*((_QWORD *)ptr + N)` / `*((_DWORD *)ptr + N)` (IDA types)
   - `*((int *)ptr + N)` / `*((long long *)ptr + N)` (standard C types)
   - `*((void **)ptr + N)` / `*((char **)ptr + N)` (nested pointer PAMA)
   - `*(_DWORD *)(base + offset)` (offset-based access)
   - `void *` variables used with pointer-arithmetic member access
   - `_UNKNOWN *` types

2. For each PAMA pattern found:
   a. Identify the base pointer variable
   b. Compute byte offsets using: byte_offset = index * sizeof(cast_type)
   c. Collect all accesses to the same base pointer
   d. Infer the struct layout (sort by byte offset, determine field types)
   e. Define a `typedef struct { ... } name_t;` before the first use
   f. Replace ALL PAMA expressions with named member access (e.g., `ptr->field`)
   g. Update variable declarations (e.g., `void *ptr` → `name_t *ptr`)
   h. Compile and verify after EACH struct definition

3. Add `// Restored:` comments on every struct definition and every converted member access.

### IMPORTANT — Array-to-Struct Unflattening:
When a local array like `_QWORD dest[5]` shows heterogeneous usage:
- Base used as byte buffer: `strcpy((char *)dest, src)`
- Specific indices cast to pointers: `(const char *)(dest[2])`
- Index-specific byte manipulation: `HIBYTE(dest[1]) = 0`
→ Convert to a struct. Compute offsets carefully.

### Byte Offset Calculation (MANDATORY):
- `*((_QWORD *)ptr + 2)` → sizeof(_QWORD) = 8 → byte offset = 2*8 = 16
- `*((_DWORD *)ptr + 3)` → sizeof(_DWORD) = 4 → byte offset = 3*4 = 12
- `*((char **)ptr + 1)` → sizeof(char *) = 8 (pointer!) → byte offset = 1*8 = 8
- For offset-based: `*(_DWORD *)((char *)base + 8)` → byte offset = 8

### Anti-Hallucination:
- Every field MUST be backed by a concrete PAMA expression in the code.
- Every type inference MUST cite evidence in `// Restored:` comments.
- If uncertain, keep the original PAMA — do NOT guess.

### Compilation:
Use the compile command shown in the Parse GCC Errors tool description.

Report: number of structs defined, PAMA expressions replaced, and final compilation status.
If no PAMA/degradation detected, report skip reason.
"""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_struct_restore(
    base_dir: str,
    c_file: str,
    compile_cmd: str = DEFAULT_COMPILE_CMD,
    max_iterations: int = 500,
    verbose: bool = True,
) -> Tuple[bool, str]:
    """Run Phase 2: struct & type restoration.

    Returns:
        (success: bool, message: str)
    """
    filepath = os.path.join(base_dir, c_file)

    # Gate check
    should_run, reason = gate_check(filepath)
    if not should_run:
        cprint(f"[Phase 2] SKIPPED: {reason}", color="yellow")
        return True, f"SKIPPED: {reason}"

    cprint(f"[Phase 2] RUNNING: {reason}", color="yellow")

    steps_log_path = os.path.join(
        base_dir,
        f"phase2_struct_restore_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
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
        save_agent_result(base_dir, summary, agent_name="struct_restore")

        if result.get("status") == "success":
            return True, result.get("output", "Struct restoration succeeded.")
        return False, result.get("output", "Unknown error.")

    except Exception as e:
        summary = {
            "log_path": steps_log_path,
            "status": "failure",
            "message": str(e),
        }
        save_agent_result(base_dir, summary, agent_name="struct_restore")
        return False, str(e)
    finally:
        step_logger.close()
