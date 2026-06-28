"""
Phase 3: Function Signature Restoration Agent

Restores degraded function signatures in decompiled C code.
Recovers return types, parameter types, parameter names, calling
conventions, and dropped parameters (signature downgrade from UB).

Corresponds to the `restore-function-signatures` skill.
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
# Gate check — generic signatures or degraded calls
# ---------------------------------------------------------------------------

_SIGNATURE_DEGRADATION_PATTERNS = [
    # D1: Generic return types
    r'^__int64\s+\w+\(',           # __int64 return
    r'^unsigned\s+__int64\s+\w+\(', # unsigned __int64 return
    r'^__int32\s+\w+\(',            # __int32 return
    # D2: Generic parameter types
    r'\(.*__int64\s+\w+',           # __int64 param
    r'\(.*unsigned\s+__int64\s+\w+', # unsigned __int64 param
    r'\(.*_QWORD\s+\w+',            # _QWORD param
    # D3: Generic parameter names (a1-aN)
    r'\([^)]*\ba\d+\b',             # a1, a2, etc. as params
    # D4: Calling convention issues
    r'__fastcall\s+\w+\s*\w+\s*\(',  # __fastcall (redundant on x64)
    r'__stdcall\s+\w+\s*\w+\s*\(',   # __stdcall
    r'__cdecl\s+\w+\s*\w+\s*\(',     # __cdecl
    # D5: Dropped parameters
    r'\(\(\s*\w+\s*\(\s*\*\s*\)\s*\(\s*\)\s*\)\s*\w+\s*\)\s*\(',  # ((ret (*)())func)()
]


def gate_check(filepath: str) -> Tuple[bool, str]:
    """Check for generic function signatures or degraded calls."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return False, f"Cannot read file: {filepath}"

    matched = []
    for pattern in _SIGNATURE_DEGRADATION_PATTERNS:
        if re.search(pattern, content, re.MULTILINE):
            matched.append(pattern)

    if matched:
        return True, f"Signature degradation detected: {len(matched)} pattern(s)"
    return False, "No signature degradation detected"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a ReAct-style agent for restoring degraded function signatures in decompiled C pseudocode.
Your job is to recover correct return types, parameter types, parameter names, calling conventions,
and dropped parameters across 5 dimensions.

## Supreme Rule: Memory Semantics Above All Syntax Rules
- Preserve original memory read/write paths, dereference levels, and data-flow edges.
- Do NOT add `&` to "fix" type mismatches.
- Do NOT change pointer dereference depth.

## 5 Dimensions of Restoration

### D1: Return Type Recovery
Generic return types (__int64, __int32, unsigned __int64) → correct C types.
| Evidence in function body | Likely return type |
|---|---|
| `return 0;` / `return -1;` on success/failure | int, BOOL |
| `return ptr;` where ptr is void */char * | matching pointer type |
| Callers assign return to typed variable | use that variable's type |
| Callers pass return to known API | use that API's expected param type |

Known: malloc → void *, strlen → size_t, open/socket → int (fd), printf → int.

### D2: Parameter Type Recovery
Generic param types (__int64, _QWORD, unsigned __int64) → correct types.
| Usage pattern in body | Likely type |
|---|---|
| Passed to strlen/strcmp/strcpy | const char * |
| Passed to memcpy/read/write as buffer | void * or char * |
| Passed to free() | void * |
| Null-checked with error return | pointer |
| Dereferenced with *param or param->field | pointer to dereferenced type |
| Used as loop bound / array subscript | int or size_t |
| Cast to pointer type before use | that pointer type |

ABI constraint: On 64-bit, only _QWORD (8 bytes) can hold a pointer. _DWORD (4 bytes) cannot.

### D3: Parameter Name Recovery
Generic names (a1-aN) → semantic names derived from usage.
| Role | Signals | Candidate names |
|---|---|---|
| String input | strlen/strcmp/strcpy/printf("%s") | str, input, name, path, message |
| Buffer dest | memcpy/strcpy/read dest, malloc assigned | buf, dest, out, data |
| Length/Size | malloc arg, memcpy count, loop bound | len, size, count, n |
| File descriptor | read/write/close first arg | fd, sockfd |
| Opaque context | passed through, rarely dereferenced | ctx, handle, conn |

### D4: Calling Convention Cleanup
- On x86-64: remove __fastcall/__stdcall/__cdecl — they are redundant. The ABI is implicit.
- On x86: variadic functions MUST be __cdecl. DLL exports often need __stdcall.
- __thiscall only on C++ member functions.

### D5: Dropped Parameter Recovery (Signature Downgrade)
Pattern: ((ret_type (*)())func)() — function forced to zero arguments.
1. Find the true signature (extern declaration, call sites, or known API).
2. Declare UNINIT_RECOVERED_arg<N> dummy variables for each dropped parameter.
3. Do NOT initialize them — preserving CWE-457 data flow is the goal.
4. Remove the inline cast, restore normal call with all arguments.
5. Mark: `// Restored AST: UB parameter loss recovered`.

## Rules
- Evidence-gated: apply only at HIGH confidence. When ambiguous, preserve original.
- Mark all changes with `// Recovered:` comments citing specific evidence.
- No logic alteration: only change signature lines and related declarations.
- Fix forward: if compilation breaks, fix with casts — do NOT revert recovery.
- Dimension independence: success/failure in one dimension does not affect others.

Prefer `Read Code Slice` + `Patch Apply` for targeted changes.
After each batch of changes, recompile to verify.
"""


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------

TASK_PROMPT = """\
Your task is to scan all function definitions in the .c file(s) and restore degraded signatures.

### Detection: For each function, check these 5 dimensions:

D1 - Generic return type: __int64, unsigned __int64, __int32
D2 - Generic parameter types: __int64, _QWORD, unsigned __int64, int used as pointer
D3 - Generic parameter names: a1, a2, a3, ... (decompiler defaults)
D4 - Calling convention: __fastcall/__stdcall/__cdecl (redundant on x64, check correctness on x86)
D5 - Dropped parameters: ((ret_type (*)())func)() inline casts stripping arguments

### Cross-Validation Priority:
1. Standard library / well-known API (if function name matches, use documented signature)
2. Extern declaration in the same file
3. Call sites — how the function is called, what types are passed, how return is used
4. Function body — how parameters and return value are used internally
5. Domain / API ecosystem context

### Restoration Order (per function):
D4 (convention) → D1 (return type) → D2 (param types) → D3 (param names)
Process D5 (dropped params) LAST — it operates on call sites, not definitions.

### Multi-Function Coordination:
When foo calls bar, restore bar's signature first (call site evidence in foo is more reliable).
When a function's signature changes, update all its call sites and declarations.

### For D5 (Dropped Parameters):
1. Search for the true signature (extern declaration, known API, or call sites in other files)
2. If no true signature can be found → SKIP. Report: "Cannot determine true signature."
3. Compare parameter counts: if equal → NOT a downgrade, skip. If true has MORE → proceed.
4. Declare UNINIT_RECOVERED_arg<N> (N = 1-based position) with the true type.
5. Do NOT initialize the variables. Add __attribute__((unused)) if needed.
6. Mark: `// Restored AST: UB parameter loss recovered`.

### Compilation:
Use the compile command shown in the Parse GCC Errors tool description.

Report: return types, param types, param names, calling conventions, and dropped parameters restored.
If no degradation detected, report skip reason.
"""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_function_signature_restore(
    base_dir: str,
    c_file: str,
    compile_cmd: str = DEFAULT_COMPILE_CMD,
    max_iterations: int = 500,
    verbose: bool = True,
) -> Tuple[bool, str]:
    """Run Phase 3: function signature restoration.

    Returns:
        (success: bool, message: str)
    """
    filepath = os.path.join(base_dir, c_file)

    # Gate check
    should_run, reason = gate_check(filepath)
    if not should_run:
        cprint(f"[Phase 3] SKIPPED: {reason}", color="yellow")
        return True, f"SKIPPED: {reason}"

    cprint(f"[Phase 3] RUNNING: {reason}", color="yellow")

    steps_log_path = os.path.join(
        base_dir,
        f"phase3_function_signature_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
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
        save_agent_result(base_dir, summary, agent_name="function_signature")

        if result.get("status") == "success":
            return True, result.get("output", "Function signature restoration succeeded.")
        return False, result.get("output", "Unknown error.")

    except Exception as e:
        summary = {
            "log_path": steps_log_path,
            "status": "failure",
            "message": str(e),
        }
        save_agent_result(base_dir, summary, agent_name="function_signature")
        return False, str(e)
    finally:
        step_logger.close()
