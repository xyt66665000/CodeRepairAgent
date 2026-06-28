"""
Phase 5: Control Flow Normalizer Agent

Normalizes decompiler-produced goto/label spaghetti into structured control
flow: goto→while/for, flattened CFG recovery, switch reconstruction,
break/continue restoration.

Corresponds to the `control-flow-normalizer` skill.
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
# Gate check — goto/label patterns
# ---------------------------------------------------------------------------

_GOTO_LABEL_PATTERNS = [
    r'\bgoto\b\s+\w+',          # goto statement
    r'^\w+:\s*$',               # label definition (simplified)
    r'^\s*\w+\s*:\s*$',         # label definition (with indentation)
    r'LABEL_\d+\s*:',           # LABEL_N: pattern
    r'CASE_\d+\s*:',            # CASE_N: pattern
]


def gate_check(filepath: str) -> Tuple[bool, str]:
    """Check for goto/label patterns in the file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return False, f"Cannot read file: {filepath}"

    # Count goto statements
    goto_count = len(re.findall(r'\bgoto\b\s+\w+', content))
    # Count labels
    label_count = len(re.findall(r'^\s*\w+\s*:\s*$', content, re.MULTILINE))

    if goto_count > 0 or label_count > 0:
        return True, f"Goto/label patterns detected: {goto_count} goto(s), {label_count} label(s)"
    return False, "No goto/label patterns detected"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a ReAct-style agent for normalizing decompiler-produced goto/label spaghetti
into structured control flow in C pseudocode.

## Supreme Rule: Memory Semantics Above All Syntax Rules
- Control flow restructuring MUST NOT alter any expression evaluation order that could affect memory access.
- The sequence of reads and writes must be identical.
- Do not reorder statements across goto boundaries.
- Do not merge/split basic blocks in ways that change evaluation order.
- Preserve all side effects in their original positions.

## Pattern Recognition & Recovery Rules

### Rule 1: While Loop Recovery
```
LABEL_N:
  if (!cond) goto LABEL_EXIT;
  <body>
  goto LABEL_N;
LABEL_EXIT:
```
→ `while (cond) { <body> }`

Variant — while(1) + mid-body break:
```
LABEL_N:
  <body_part_1>
  if (exit_cond) goto LABEL_EXIT;
  <body_part_2>
  goto LABEL_N;
LABEL_EXIT:
```
→ `while (1) { <body_part_1>; if (exit_cond) break; <body_part_2>; }`

### Rule 2: Do-While Loop Recovery
```
LABEL_N:
  <body>
  if (cond) goto LABEL_N;
```
→ `do { <body> } while (cond);`

### Rule 3: For Loop Recovery
```
  v = 0;
  goto LABEL_CHECK;
LABEL_BODY:
  <body>
  v++;
LABEL_CHECK:
  if (v < limit) goto LABEL_BODY;
```
→ `for (v = 0; v < limit; v++) { <body> }`

### Rule 4: If-Else Recovery
```
  if (!cond) goto LABEL_ELSE;
  <then_body>
  goto LABEL_END;
LABEL_ELSE:
  <else_body>
LABEL_END:
```
→ `if (cond) { <then_body> } else { <else_body> }`

### Rule 5: Break Recovery
Only apply after enclosing loop is identified. Forward goto inside loop to loop exit → break.

### Rule 6: Continue Recovery
Only apply after enclosing loop is identified. Forward goto inside loop to loop header → continue.

### Rule 7: Switch-Case Recovery
```
  if (v == 0) goto CASE_0;
  if (v == 1) goto CASE_1;
  if (v == 2) goto CASE_2;
  goto CASE_DEFAULT;
CASE_0: ... goto CASE_END;
CASE_1: ... goto CASE_END;
CASE_2: ... goto CASE_END;
CASE_DEFAULT: ...
CASE_END:
```
→ `switch (v) { case 0: ... break; case 1: ... break; ... default: ... break; }`

Fall-through detection: if a case body lacks `goto CASE_END`, it falls through.
Preserve with `// Restored: fall-through` comment. Do NOT add break.

### Rule 9: Short-Circuit AND/OR Recovery
AND pattern (both must be true):
```
  if (!A) goto LABEL_END;
  if (!B) goto LABEL_END;
  <then_body>
LABEL_END:
```
→ `if (A && B) { <then_body> }`

OR pattern (either true):
```
  if (A) goto LABEL_BODY;
  if (B) goto LABEL_BODY;
  goto LABEL_END;
LABEL_BODY:
  <then_body>
LABEL_END:
```
→ `if (A || B) { <then_body> }`

### Rule 10: Ternary Operator Recovery
```
  if (!cond) goto LABEL_FALSE;
  v1 = a;
  goto LABEL_MERGE;
LABEL_FALSE:
  v1 = b;
LABEL_MERGE:
```
→ `v1 = cond ? a : b;`

Requirements: both branches assign to same variable, no side effects, control merges immediately.

## Non-Negotiable Rules
1. Confidence-gated: only apply recovery at HIGH confidence. Leave ambiguous gotos with annotation.
2. Innermost-first ordering: recover innermost structures first, work outward.
3. Single-pass scope: only recover gotos within same function (≤100 lines span).
4. Never invent logic: pure restructuring only. Only allowed changes:
   - Remove goto statements and LABEL_N: markers
   - Add while/for/do-while/switch/if-else keywords and braces
   - Add break/continue
   - Combine consecutive if conditions with &&/||
   - Merge same-variable assignments into ternary ?:
   - Adjust indentation
   - Add `// Restored:` comments

Prefer `Read Code Slice` + `Patch Apply` for targeted restructuring.
After changes, recompile to verify.
"""


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------

TASK_PROMPT = """\
Your task is to normalize goto/label patterns in the .c file(s) into structured control flow.

### Steps:
1. SCAN the file for:
   - Backward goto to a label above → loop candidate
   - Forward goto targeting loop header/exit → continue/break candidate
   - Chain of `if (v == const) goto CASE_N` → switch-case candidate
   - Consecutive `if (!A) goto SAME; if (!B) goto SAME;` → short-circuit candidate
   - Both if-else branches assign same variable → ternary candidate
   - Sequential labels with interleaved gotos → flattened CFG

2. CLASSIFY each label region using the 10 pattern rules.

3. ORDER regions innermost-first by label nesting depth.

4. For EACH region (innermost first):
   a. MATCH → Apply the appropriate recovery rule
   b. Replace goto/label with structured construct
   c. Add `// Restored:` comment
   d. Update remaining regions (outer structures may now be recognizable)
   e. If AMBIGUOUS → skip, add `// Restored: ambiguous — ...` comment

5. VERIFY: recompile with strict gcc after all restorations.

### Marking:
- Loop: `// Restored: goto loop → while/for/while(1) (@LABEL_N..LABEL_M)`
- Break: `// Restored: goto LABEL_EXIT → break`
- Continue: `// Restored: goto LABEL_HEADER → continue`
- Switch: `// Restored: if-goto chain → switch (v)`
- If-else: `// Restored: goto branch → if-else`
- Short-circuit: `// Restored: consecutive if-goto → if (A && B)`
- Ternary: `// Restored: if-else goto → ternary (v1 = cond ? a : b)`

### Confidence Rules:
- Only apply recovery when HIGH confidence.
- Ambiguous patterns → preserve goto, annotate.
- Unrecognized patterns → leave as-is (state machines, long-distance gotos for error cleanup).

### Compilation:
Use the compile command shown in the Parse GCC Errors tool description.

Report: loops, switches, if-else, short-circuit, ternary recovered, gotos preserved (ambiguous).
If no goto/label patterns, report skip reason.
"""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_control_flow_normalize(
    base_dir: str,
    c_file: str,
    compile_cmd: str = DEFAULT_COMPILE_CMD,
    max_iterations: int = 500,
    verbose: bool = True,
) -> Tuple[bool, str]:
    """Run Phase 5: control flow normalization.

    Returns:
        (success: bool, message: str)
    """
    filepath = os.path.join(base_dir, c_file)

    # Gate check
    should_run, reason = gate_check(filepath)
    if not should_run:
        cprint(f"[Phase 5] SKIPPED: {reason}", color="yellow")
        return True, f"SKIPPED: {reason}"

    cprint(f"[Phase 5] RUNNING: {reason}", color="yellow")

    steps_log_path = os.path.join(
        base_dir,
        f"phase5_control_flow_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
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
        save_agent_result(base_dir, summary, agent_name="control_flow")

        if result.get("status") == "success":
            return True, result.get("output", "Control flow normalization succeeded.")
        return False, result.get("output", "Unknown error.")

    except Exception as e:
        summary = {
            "log_path": steps_log_path,
            "status": "failure",
            "message": str(e),
        }
        save_agent_result(base_dir, summary, agent_name="control_flow")
        return False, str(e)
    finally:
        step_logger.close()
