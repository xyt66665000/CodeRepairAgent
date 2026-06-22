---
name: restore-control-flow
description: Normalize decompiler-produced goto/label spaghetti into structured control flow (goto→while/for, flattened CFG recovery, switch reconstruction, break/continue restoration). Triggers on "restore control flow", "normalize goto", "fix CFG", "control flow recovery", "control-flow-normalizer".
version: 1.0.0
---

# Control Flow Normalizer

Recover structured control flow from decompiled C pseudocode where the decompiler emitted `goto`/`label` spaghetti instead of `while`, `for`, `do-while`, `switch`, `break`, and `continue`.

---

## CRITICAL: Memory Semantics Above All Syntax Rules

**This section is the supreme rule. It overrides ALL other repair rules when there is a conflict.**

Decompiled code has pervasive type loss — variables that were originally pointers are often recognized as `_QWORD`, `int`, `unsigned __int64`, or other scalar types by the decompiler. Your task is to restore the **underlying memory logic**, NOT to tamper with that logic to eliminate compiler warnings. All repairs must preserve the original memory read/write paths, dereference levels, and data-flow edges — no adding `&` to "fix" a type mismatch, no changing pointer dereference depth, no altering the physical stack or heap access patterns.

---

## CRITICAL: File Integrity — NEVER Truncate the Target File

**The `Write` tool overwrites the ENTIRE file.** If you read only part of a large file then use `Write`, the unread portion is destroyed. This is the #1 cause of catastrophic data loss in automated repair.

### Mandatory Rules

1. **Read the ENTIRE file before any Write.** Use `Read` without offset/limit. If the file is too large, use `Edit` exclusively.
2. **Prefer `Edit` over `Write` for ALL changes.** Goto/label restructuring MUST use `Edit` for targeted replacements. `Write` is a last resort.
3. **After every modification, verify:** `test -s <file>.c` (non-empty check), then `wc -l <file>.c` (sanity check line count).

---

## Core Objective

**Given** compilable decompiled C code littered with `goto LABEL_N` and `LABEL_N:` markers, **recover** structured control flow constructs (`while`, `for`, `do-while`, `switch-case`, `break`, `continue`) so the code is semantically readable and analyzable by SAST tools.

---

## Detection: When to Trigger

Activate this skill when the code exhibits **any** of:

| # | Pattern | Signal |
|---|---------|--------|
| 1 | Backward `goto` to a label above | Cyclic control flow → loop candidate |
| 2 | `goto` targeting a label immediately after the current block | `break` candidate |
| 3 | `goto` targeting a loop header label from within the loop body | `continue` candidate |
| 4 | Chain of `if (v == const) goto CASE_N` | `switch-case` candidate |
| 5 | `if (!cond) goto ELSE; ... goto END; ELSE: ... END:` | if-else flattening |
| 6 | Sequential labels with `goto` between them (no structured loops) | Flattened CFG |
| 7 | Consecutive `if (!A) goto LABEL_X; if (!B) goto LABEL_X;` (same target) | Short-circuit AND/OR candidate |
| 8 | Loop body with a mid-body break: `...; if (cond) goto EXIT; ...; goto TOP;` | `while(1) + break` candidate |
| 9 | If-else where both branches only assign to the same variable | Ternary operator candidate |

---

## Pattern Recognition & Recovery Rules

### Rule 1: While Loop Recovery

**Pattern**: A label before a condition check, with a backward goto from below.

```
LABEL_N:
  if (!cond)
    goto LABEL_EXIT;
  <body>
  goto LABEL_N;
LABEL_EXIT:
```

**Recovery**:
```c
while (cond) {
  <body>
}
```

**Confidence**: HIGH when the backward goto is the only incoming edge to LABEL_N (from below) and the condition is a direct branch to exit.

**Variant — `while(1)` + mid-body break**: When the backward goto is unconditional (not guarded by a condition) but a forward conditional goto to LABEL_EXIT sits in the middle of the body, this is a `while(1)` with an internal escape:

```
LABEL_N:
  <body_part_1>
  if (exit_cond)
    goto LABEL_EXIT;
  <body_part_2>
  goto LABEL_N;
LABEL_EXIT:
```

**Recovery**:
```c
while (1) {
  <body_part_1>
  if (exit_cond)
    break;
  <body_part_2>
}
```

**Confidence**: HIGH when the backward goto is unconditional AND exactly one forward conditional goto targets the loop exit from the middle of the body. In `while(1)` form, the original exit condition is preserved verbatim inside the loop — the decompiler already expressed it correctly; only the framing changes.

### Rule 2: Do-While Loop Recovery

**Pattern**: A label at the top, with a conditional backward goto at the bottom.

```
LABEL_N:
  <body>
  if (cond)
    goto LABEL_N;
```

**Recovery**:
```c
do {
  <body>
} while (cond);
```

**Confidence**: HIGH when the backward goto is the last statement in the block and no other edges target LABEL_N.

### Rule 3: For Loop Recovery

**Pattern**: Init before a check-label, body with an increment, backward goto to the check.

```
  v = 0;
  goto LABEL_CHECK;
LABEL_BODY:
  <body>
  v++;
LABEL_CHECK:
  if (v < limit)
    goto LABEL_BODY;
```

**Recovery**:
```c
for (v = 0; v < limit; v++) {
  <body>
}
```

**Confidence**: HIGH when all three components (init, condition, increment) are clearly identifiable by dataflow and the loop counter is not modified inside the body. MEDIUM if only init+condition or condition+increment are present.

### Rule 4: If-Else Recovery

**Pattern**: Conditional goto to else block, then unconditional goto to skip else.

```
  if (!cond)
    goto LABEL_ELSE;
  <then_body>
  goto LABEL_END;
LABEL_ELSE:
  <else_body>
LABEL_END:
```

**Recovery**:
```c
if (cond) {
  <then_body>
} else {
  <else_body>
}
```

**Confidence**: HIGH when both goto targets are forward, LABEL_ELSE is only reached via the conditional goto, and LABEL_END follows immediately after else_body.

**Edge case — if without else**:
```
  if (!cond)
    goto LABEL_END;
  <then_body>
LABEL_END:
```
→
```c
if (cond) {
  <then_body>
}
```

### Rule 5: Break Recovery

**Pattern**: A forward `goto` from inside a loop body to the statement immediately after the loop.

Do NOT recover in isolation. Only apply after the enclosing loop has been identified (Rule 1-3). The goto target must be the first statement after the loop exit label.

```
while (cond) {
  if (err)
    goto LABEL_EXIT;   // → break
  <body>
}
```

**Confidence**: HIGH when the goto target is the loop exit label and the goto is inside a conditional within the loop body.

### Rule 6: Continue Recovery

**Pattern**: A forward `goto` from inside a loop body to the loop header label (condition check).

Same constraint as break — only apply after the enclosing loop has been identified.

```
while (cond) {
  if (skip)
    goto LABEL_HEADER;  // → continue
  <body>
}
```

**Confidence**: HIGH when the goto target is exactly the loop header label and the goto is inside a conditional within the loop body.

### Rule 7: Switch-Case Recovery

**Pattern**: A chain of `if (v == const) goto CASE_N` or a jump table.

```
  if (v == 0) goto CASE_0;
  if (v == 1) goto CASE_1;
  if (v == 2) goto CASE_2;
  goto CASE_DEFAULT;
CASE_0:
  <body_0>
  goto CASE_END;
CASE_1:
  <body_1>
  goto CASE_END;
CASE_2:
  <body_2>
  goto CASE_END;
CASE_DEFAULT:
  <default_body>
CASE_END:
```

**Recovery**:
```c
switch (v) {
  case 0:
    <body_0>
    break;
  case 1:
    <body_1>
    break;
  case 2:
    <body_2>
    break;
  default:
    <default_body>
    break;
}
```

**Confidence**: HIGH when ≥2 consecutive `if (same_var == const) goto CASE_N` comparisons exist, all comparing the same variable against compile-time constants. **Fall-through detection**: if a case body lacks a `goto CASE_END`, it falls through to the next case — preserve this behavior with a `// Restored: fall-through` comment (do NOT add `break`).

### Rule 8: Flattened CFG Recovery

**Pattern**: Multiple sequential labels where control flows via goto in a structured pattern that the decompiler failed to nest.

**Approach**:
1. Mark each label as a basic block entry point
2. Build the control-flow graph (which block jumps to which)
3. Identify structured subgraphs: if-then diamonds, loop back-edges, n-way switch dispatches
4. Apply Rules 1-10 to recover each subgraph, working from innermost to outermost
5. Replace basic-block gotos with structured constructs

**Confidence**: Apply per-subgraph using the confidence rules above. Only recover subgraphs that match a known structured pattern. Leave unrecognized gotos in place.

### Rule 9: Logical AND/OR Recovery (Short-Circuit Evaluation)

**Pattern**: Consecutive conditional `goto` statements that target the **same exit label**, short-circuiting evaluation. The decompiler linearizes `if (A && B)` or `if (A || B)` into sequential checks.

**AND pattern** — both conditions must be true to enter the body:
```
  if (!A)
    goto LABEL_END;
  if (!B)
    goto LABEL_END;
  <then_body>
LABEL_END:
```

**Recovery**:
```c
if (A && B) {
  <then_body>
}
```

**OR pattern** — either condition being true enters the body:
```
  if (A)
    goto LABEL_BODY;
  if (B)
    goto LABEL_BODY;
  goto LABEL_END;
LABEL_BODY:
  <then_body>
LABEL_END:
```

**Recovery**:
```c
if (A || B) {
  <then_body>
}
```

**Confidence**:
- HIGH when ≥2 consecutive `if` statements target the **same label**, comparing distinct conditions, with no intervening statements (except other same-target conditionals)
- AND vs. OR disambiguation: negated conditions (`!A`, `A == 0`) targeting the skip-label → AND. Positive conditions targeting the body-label → OR.
- MEDIUM when conditions are separated by ≤2 simple assignment statements (decompiler artifacts), as long as those assignments do not modify variables used in subsequent conditions

**Nested short-circuit**: When AND and OR patterns interleave (e.g., `if (!A) goto END; if (B) goto BODY; if (!C) goto END;`), this represents `if (A && (B || C))` or similar nesting. Recover innermost groups first, then combine.

**Recovery procedure**:
1. Scan for consecutive `if (cond) goto SAME_TARGET` patterns
2. Group all conditions sharing the same target label
3. For AND (negated conditions → skip label): join with `&&`
4. For OR (positive conditions → body label): join with `||`
5. Preserve short-circuit order — the first condition in the source is the first in the compound expression

### Rule 10: Ternary Operator Recovery

**Pattern**: An if-else branch pair where **both branches exclusively assign to the same variable**, and control merges immediately after.

```
  if (!cond)
    goto LABEL_FALSE;
  v1 = a;
  goto LABEL_MERGE;
LABEL_FALSE:
  v1 = b;
LABEL_MERGE:
```

**Recovery**:
```c
v1 = cond ? a : b;
```

**Confidence**: HIGH when ALL of the following hold:
1. Both the then-branch and else-branch contain exactly ONE assignment (may be preceded/succeeded by other statements, but the core assignment targets the same variable)
2. The assignment target variable (`v1`) is identical in both branches
3. The control flow merges at a label immediately after the else-branch assignment
4. No side effects exist in either branch beyond the assignment (no function calls, no memory writes to other locations)

**Edge case — multi-statement branches**: When branches contain more than just an assignment, do NOT recover as ternary. A ternary encodes a pure value selection; multi-statement branches carry control-flow semantics that a ternary would obscure.

**Edge case — nested ternary**: When `a` or `b` are themselves ternary-recoverable patterns, nest them: `v1 = cond1 ? (cond2 ? x : y) : b`. Always parenthesize nested ternaries.

---

## Non-Negotiable Rules

### Rule A: Memory Semantics Above All

Control flow restructuring MUST NOT alter any expression evaluation order that could affect memory access. The sequence of reads and writes must be identical. In particular:
- Do not reorder statements across goto boundaries
- Do not merge or split basic blocks in ways that change evaluation order
- Preserve all side effects in their original positions

### Rule B: Confidence-Gated Recovery

Only apply a recovery when confidence is HIGH. If a goto pattern is ambiguous (matches multiple structures, or has irregular edge cases), leave it as-is with an annotation:

```c
// Restored: ambiguous — could be while-loop or state-machine transition
goto LABEL_12;
```

### Rule C: Innermost-First Ordering

Always recover innermost structures first, then work outward. Recovering an inner loop may reveal the structure of its outer loop.

### Rule D: Single-Pass Scope Boundary

Only recover gotos where all involved labels are within the **same function** and the control flow is **locally analyzable** (all labels and gotos visible within a contiguous span of ≤100 lines). Cross-function or long-distance gotos (e.g., error-cleanup patterns spanning 100+ lines) should be preserved.

### Rule E: Mark All Changes

Every recovery must be marked with a comment:

- Loop recovery: `// Restored: goto loop → while/for/while(1) (@original labels LABEL_N..LABEL_M)`
- Break/continue: `// Restored: goto LABEL_EXIT → break`
- Switch: `// Restored: if-goto chain → switch (v)`
- If-else: `// Restored: goto branch → if-else`
- Short-circuit: `// Restored: consecutive if-goto → if (A && B)` or `// Restored: consecutive if-goto → if (A || B)`
- Ternary: `// Restored: if-else goto → ternary (v1 = cond ? a : b)`

### Rule F: Compilation Verification

After recovery, recompile with the standard strict command:

```bash
gcc -c -Werror=implicit-function-declaration -Werror=implicit-int \
    -Werror=incompatible-pointer-types -Werror=int-conversion \
    -Werror=return-type -fno-builtin -fmax-errors=0 -I.
```

New errors from restructuring → fix forward (missing semicolons, scope issues). Do NOT revert recovery.

### Rule G: Never Invent Logic

Control flow recovery must be a pure restructuring — no adding, removing, or modifying statements. The only allowed changes are:
- Removing `goto` statements and `LABEL_N:` markers
- Adding `while`/`for`/`do-while`/`switch`/`if-else` keywords and braces
- Adding `break`/`continue`
- Combining consecutive `if` conditions with `&&`/`||` (Rule 9)
- Merging same-variable assignments into ternary `?:` expressions (Rule 10)
- Adjusting indentation
- Adding `// Restored:` comments

---

## Recovery Workflow

```
INPUT: compilable .c file with goto/label patterns
  │
  ▼
SCAN — Collect all labels and goto targets
  │  Build label→goto mapping, identify basic block boundaries
  │
  ▼
DETECT — Classify each label region
  │  │  Backward goto? → loop candidate (Rule 1, 2, 3)
  │  │  Forward goto to block end? → if-else candidate (Rule 4)
  │  │  Chained if (v==const) goto? → switch candidate (Rule 7)
  │  │  Consecutive if-goto to same target? → short-circuit candidate (Rule 9)
  │  │  Both if-else branches assign same var? → ternary candidate (Rule 10)
  │  │  Goto to loop-header from within body? → continue candidate (Rule 6)
  │  │  Goto to loop-exit from within body? → break candidate (Rule 5)
  │  │  Unrecognized pattern → SKIP, preserve goto
  │
  ▼
ORDER — Sort regions innermost-first by label nesting depth
  │
  ▼
For EACH region (innermost first):
  │
  ├─ MATCH → Apply recovery rule
  │    │  Replace goto/label with structured construct
  │    │  Add // Restored: comment
  │    │  Update label→goto mapping for remaining regions
  │    │
  │    └─ Outer loops may now be recognizable → continue iteration
  │
  ├─ AMBIGUOUS → Skip, annotate
  │
  ▼
VERIFY — Recompile with strict gcc
  │  Fix any introduced errors (do NOT revert)
  │
  ▼
DONE
```

---

## Examples

### Example 1: While Loop

**Before:**
```c
LABEL_5:
  if (v1 >= v3)
    goto LABEL_8;
  v4 = buf[v1];
  if (v4 == 0x0A)
    goto LABEL_8;
  v1 = v1 + 1;
  goto LABEL_5;
LABEL_8:
```

**After:**
```c
// Restored: goto loop → while (@LABEL_5..LABEL_8)
while (v1 < v3) {
  v4 = buf[v1];
  if (v4 == 0x0A)
    break;
  v1 = v1 + 1;
}
```

### Example 2: For Loop

**Before:**
```c
  v2 = 0;
  goto LABEL_6;
LABEL_4:
  out[v2] = in[v2] ^ 0x55;
  v2 = v2 + 1;
LABEL_6:
  if (v2 < v1)
    goto LABEL_4;
```

**After:**
```c
// Restored: goto loop → for (@LABEL_4..LABEL_6)
for (v2 = 0; v2 < v1; v2 = v2 + 1) {
  out[v2] = in[v2] ^ 0x55;
}
```

### Example 3: If-Else

**Before:**
```c
  if (v1 != 0)
    goto LABEL_10;
  v2 = process_a();
  goto LABEL_12;
LABEL_10:
  v2 = process_b();
LABEL_12:
  return v2;
```

**After:**
```c
  // Restored: goto branch → if-else
  if (v1 == 0) {
    v2 = process_a();
  } else {
    v2 = process_b();
  }
  return v2;
```

### Example 4: Switch-Case

**Before:**
```c
  if (v1 == 0) goto CASE_0;
  if (v1 == 1) goto CASE_1;
  if (v1 == 2) goto CASE_2;
  goto CASE_DEFAULT;
CASE_0:
  v2 = 100;
  goto CASE_END;
CASE_1:
  v2 = 200;
  goto CASE_END;
CASE_2:
  v2 = 300;
CASE_DEFAULT:
  v2 = -1;
CASE_END:
  return v2;
```

**After:**
```c
  // Restored: if-goto chain → switch (v1)
  switch (v1) {
    case 0:
      v2 = 100;
      break;
    case 1:
      v2 = 200;
      break;
    case 2:
      v2 = 300;
      // Restored: fall-through to default
    default:
      v2 = -1;
      break;
  }
  return v2;
```

### Example 5: Do-While

**Before:**
```c
LABEL_3:
  v1 = read_byte();
  buf[v2] = v1;
  v2 = v2 + 1;
  if (v1 != 0)
    goto LABEL_3;
```

**After:**
```c
// Restored: goto loop → do-while (@LABEL_3)
do {
  v1 = read_byte();
  buf[v2] = v1;
  v2 = v2 + 1;
} while (v1 != 0);
```

### Example 6: Ambiguous — Preserve

**Before:**
```c
LABEL_15:
  v1 = *(int *)(state + 4);
  if (v1 == 0) goto LABEL_20;
  if (v1 == 1) goto LABEL_25;
  if (v1 == 2) goto LABEL_30;
  state = *(void **)(state + 8);
  goto LABEL_15;
LABEL_20:
  ...
```

**After (no change — state machine, not a simple loop):**
```c
// Restored: ambiguous — state-machine dispatch, not a recoverable structured pattern
LABEL_15:
  v1 = *(int *)(state + 4);
  if (v1 == 0) goto LABEL_20;
  if (v1 == 1) goto LABEL_25;
  if (v1 == 2) goto LABEL_30;
  state = *(void **)(state + 8);
  goto LABEL_15;
LABEL_20:
  ...
```

### Example 7: Short-Circuit AND Recovery

**Before:**
```c
  if (!ptr)
    goto LABEL_END;
  if (ptr->len == 0)
    goto LABEL_END;
  if (!ptr->data)
    goto LABEL_END;
  process(ptr->data, ptr->len);
LABEL_END:
```

**After:**
```c
  // Restored: consecutive if-goto → if (A && B && C)
  if (ptr && ptr->len != 0 && ptr->data) {
    process(ptr->data, ptr->len);
  }
```

### Example 8: while(1) + Mid-Body Break

**Before:**
```c
LABEL_4:
  v1 = read_byte();
  if (v1 == 0xFF)
    goto LABEL_8;
  buf[pos] = v1;
  pos = pos + 1;
  goto LABEL_4;
LABEL_8:
```

**After:**
```c
// Restored: goto loop → while(1) + break (@LABEL_4..LABEL_8)
while (1) {
  v1 = read_byte();
  if (v1 == 0xFF)
    break;
  buf[pos] = v1;
  pos = pos + 1;
}
```

### Example 9: Ternary Operator Recovery

**Before:**
```c
  if (v1 != 0)
    goto LABEL_TRUE;
  v2 = 10;
  goto LABEL_5;
LABEL_TRUE:
  v2 = 20;
LABEL_5:
  return v2;
```

**After:**
```c
  // Restored: if-else goto → ternary
  v2 = (v1 != 0) ? 20 : 10;
  return v2;
```

---

## Post-Recovery Compilation Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| `error: a label can only be part of a statement` | Label removed but trailing code left | Ensure the statement after a removed label is part of the new structured body |
| `error: unused label` | Label became orphaned after recovery | Remove the orphaned label |
| `error: break statement not within loop or switch` | Break used outside recovered construct | Verify the break's enclosing loop was correctly recovered; adjust braces |
| Variable scope issue | Variable declared inside a now-removed label block | Move declarations to the top of the new structured block |

---

## Final Report Format

```
=== Control Flow Normalizer Complete ===
- Labels analyzed: N
- While loops recovered: N
- For loops recovered: N
- Do-while loops recovered: N
- while(1) + break loops recovered: N
- Switch statements recovered: N
- If-else branches recovered: N
- Short-circuit AND/OR recovered: N
- Ternary operators recovered: N
- Break statements restored: N
- Continue statements restored: N
- Gotos preserved (ambiguous): N
- Compilation verified: yes / no

Recoveries:
  LABEL_5..LABEL_8 → while loop
  LABEL_4..LABEL_6 → for loop
  LABEL_10..LABEL_12 → if-else
  LABEL_4..LABEL_8 → while(1) + break
  consecutive if-goto @L12-14 → if (A && B)
  if-else goto @L20-24 → ternary (v2 = cond ? 20 : 10)
  goto CASE_END @L30 → break
  ...
```

