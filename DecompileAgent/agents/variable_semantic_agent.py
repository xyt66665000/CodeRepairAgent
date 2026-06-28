"""
Phase 4: Variable Semantic Recovery Agent

Recovers meaningful variable names and correct types for decompiler-degraded
local variables (v1→msg_len, _QWORD→void*). Uses API context, dataflow,
and usage patterns with LLM semantic understanding.

Corresponds to the `variable-semantic-recovery` skill.
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
# Gate check — generic v1..vN or scalar-in-pointer-context
# ---------------------------------------------------------------------------

_VARIABLE_DEGRADATION_PATTERNS = [
    # Generic variable names: v1, v2, ..., v99
    r'\b(v\d+)\b',
    # _DWORD / _QWORD / _BYTE / _WORD locals used in pointer contexts
    r'\b(_DWORD|_QWORD|_BYTE|_WORD)\s+\w+.*\b(strlen|malloc|free|memcpy|strcpy)\b',
    # Scalar type null-checked as pointer
    r'\b(_DWORD|_QWORD|__int64)\s+(\w+)\s*;.*\n.*if\s*\(\s*!\s*\2\s*\)',
]


def gate_check(filepath: str) -> Tuple[bool, str]:
    """Check for generic variable names or type degradation in the file.

    Specifically looks for:
    - Local variables named v1-vN
    - _DWORD / _QWORD / _BYTE / _WORD variables in pointer contexts
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return False, f"Cannot read file: {filepath}"

    # Check for generic v1-vN variable names
    v_pattern = re.findall(r'\bv\d+\b', content)
    if v_pattern:
        return True, f"Generic variable names detected: {len(set(v_pattern))} unique v1-vN names"

    # Check for scalar types in pointer contexts
    for pattern in _VARIABLE_DEGRADATION_PATTERNS[1:]:
        if re.search(pattern, content):
            return True, "Scalar type in pointer context detected"

    return False, "No generic variable names or type degradation detected"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a ReAct-style agent for recovering meaningful variable names and correct types
in decompiled C pseudocode. Your job is to rename v1-vN to semantic names and fix
degraded scalar types (_QWORD, _DWORD, int) that are actually pointers.

## Supreme Rule: Memory Semantics Above All Syntax Rules
- Preserve original memory read/write paths, dereference levels, and data-flow edges.
- Do NOT add `&` to "fix" type mismatches.
- Do NOT change pointer dereference depth.
- A rename/type-change MUST NOT alter stack offset or dereference count.

## Analysis Framework: 3 Axes

### Axis 1: Type Recovery — Is the scalar actually a pointer?
HIGH-confidence signals (need ≥2 to recover as pointer):
| # | Signal | Confidence |
|---|---|
| 1 | Assigned from malloc/calloc/mmap | HIGH |
| 2 | Passed to free() | HIGH |
| 3 | Null-checked with error/return path | HIGH |
| 4 | Explicitly dereferenced (*v, v->field) | HIGH |
| 5 | Cast to pointer type before use | HIGH |
| 6 | Passed to function expecting pointer | HIGH |
| 7 | Assigned from another pointer variable | HIGH |

ABI constraint: _DWORD (4 bytes) can only hold a pointer on 32-bit. On 64-bit, only _QWORD/__int64.

### Axis 2: Name Recovery — What semantic role?
| Role | Signals | Candidate Names |
|---|---|---|
| Length/Size | strlen/sizeof result, size arg | len, size, count, n |
| Buffer/Dest | malloc result, memcpy dest, freed | buf, dest, data, payload |
| String | strlen/strcpy/strcmp/printf("%s") | str, message, name, path |
| Loop index | for (v=0; v<limit; v++), array subscript | i, j, k, idx |
| Result/Status | function return, compared against error codes | result, ret, status, err |
| File descriptor | read/write/close first arg | fd, sockfd |
| Opaque handle | void* passed through, stored, freed | conn, ctx, handle |
| Flag/Boolean | 0/1/true/false, used in if(v) | flag, found, done, ok |

### Axis 3: API Context
| API Pattern | Type Implication |
|---|---|
| v = strlen(s) | v: size_t |
| strlen(v) | v: const char * |
| v = malloc(n) | v: void *, n: size_t |
| free(v) | v: void * |
| read(fd, buf, n) | fd: int, buf: void *, n: size_t |
| v = strdup(s) | v: char * |
| v = open(p, f) | v: int (fd) |

## Rules
1. Apply recovery ONLY at HIGH confidence. If ambiguous, preserve original and annotate.
2. Every recovery must cite at least one specific usage site: `// Recovered: v2 → message (char *) — passed to strlen(@L42)`.
3. Same-size constraint: _DWORD → char * only on 32-bit. On 64-bit, only _QWORD → pointer.
4. If mixed usage evidence → keep declared type, annotate ambiguity.
5. Never guess domain context. Generic names (buf, data, ptr) are acceptable.
6. Mark all changes with `// Recovered:` comments.

Prefer `Read Code Slice` + `Patch Apply` for targeted renames.
After changes, recompile to verify.
"""


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------

TASK_PROMPT = """\
Your task is to recover meaningful variable names and correct types in the .c file(s).

### Steps:
1. SCAN for:
   - Local variables named v1, v2, v3, ... (generic decompiler names)
   - _DWORD / _QWORD / _BYTE / _WORD variables used in pointer contexts
     (passed to strlen/malloc/free, null-checked, dereferenced)

2. For EACH candidate:
   a. Collect ALL usage sites: assignments, comparisons, function calls
   b. Analyze Axis 1 (Type): Does usage contradict the declared type?
      → If ≥2 HIGH signals → recover to correct pointer type
      → Determine pointee type from Axis 3 (API context)
   c. Analyze Axis 2 (Name): What semantic role?
      → Select best name from the role table
   d. DECIDE confidence:
      → HIGH in both → apply name + type
      → HIGH in type only → apply type, keep generic name, annotate
      → HIGH in name only → apply name, keep declared type
      → LOW in both → skip, annotate if ambiguous

3. APPLY changes using Patch Apply:
   - Type change: `_QWORD v5;` → `char *extra_copy;`
   - Name change: replace all occurrences of the variable
   - Add `// Recovered:` comment citing specific evidence

### Naming Conventions:
- Loop induction variables in small loops → single-letter (i, j, k) is fine
- Variables spanning >10 lines → descriptive multi-word name
- Multiple variables sharing same role → disambiguate (src_buf, dst_buf)
- If the function name gives domain context → derive names from that domain

### IMPORTANT — ABI Size Constraint:
- On 64-bit: only _QWORD/__int64 (8 bytes) can hold a pointer
- _DWORD (4 bytes) CANNOT hold a 64-bit pointer
- If _DWORD used in pointer contexts on 64-bit → annotate and keep, do NOT retype

### Compilation:
Use the compile command shown in the Parse GCC Errors tool description.

Report: number of names recovered, types recovered, and skipped (ambiguous).
If no generic names or type degradation, report skip reason.
"""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_variable_semantic_recovery(
    base_dir: str,
    c_file: str,
    compile_cmd: str = DEFAULT_COMPILE_CMD,
    max_iterations: int = 500,
    verbose: bool = True,
) -> Tuple[bool, str]:
    """Run Phase 4: variable semantic recovery.

    Returns:
        (success: bool, message: str)
    """
    filepath = os.path.join(base_dir, c_file)

    # Gate check
    should_run, reason = gate_check(filepath)
    if not should_run:
        cprint(f"[Phase 4] SKIPPED: {reason}", color="yellow")
        return True, f"SKIPPED: {reason}"

    cprint(f"[Phase 4] RUNNING: {reason}", color="yellow")

    steps_log_path = os.path.join(
        base_dir,
        f"phase4_variable_semantic_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
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
        save_agent_result(base_dir, summary, agent_name="variable_semantic")

        if result.get("status") == "success":
            return True, result.get("output", "Variable semantic recovery succeeded.")
        return False, result.get("output", "Unknown error.")

    except Exception as e:
        summary = {
            "log_path": steps_log_path,
            "status": "failure",
            "message": str(e),
        }
        save_agent_result(base_dir, summary, agent_name="variable_semantic")
        return False, str(e)
    finally:
        step_logger.close()
