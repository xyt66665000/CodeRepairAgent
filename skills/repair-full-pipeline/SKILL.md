---
name: repair-full-pipeline
description: Full pipeline orchestrating decompiled C code repair and semantic restoration. Executes skills in a fixed order — decompile-repair → restore-decompiled-structs → restore-function-signatures → variable-semantic-recovery → control-flow-normalizer — each gated by trigger conditions. Triggers on "repair and restore", "full pipeline", "fix and restore", "repair pipeline", "repair full pipeline".
version: 3.0.0
---

# Repair Full Pipeline

Orchestrated multi-phase pipeline for IDA Pro / Hex-Rays / Ghidra / angr decompiled C pseudocode. Each phase delegates to a dedicated skill and includes a **gate** — the phase only runs when its trigger conditions are met. All phases share the **Supreme Rule: Memory Semantics Above All Syntax Rules** (cast values, never take `&` to fix type mismatches; preserve vulnerability trigger paths; never alter program logic).

## Pipeline Order

```
INPUT: decompiled .c file
  │
  ▼
┌─────────────────────────────────────────────┐
│ PHASE 1: decompile-repair                   │
│ Make code compilable with strict gcc         │
│ Gate: always runs (entry point)              │
│ Output: .o file with zero errors             │
└─────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────┐
│ PHASE 2: restore-decompiled-structs          │
│ Restore degraded structs and eliminate PAMA  │
│ Gate: PAMA or typed degradation detected     │
└─────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────┐
│ PHASE 3: restore-function-signatures         │
│ Restore return types, param types/names,     │
│ calling conventions, dropped parameters      │
│ Gate: generic signatures or degraded calls   │
└─────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────┐
│ PHASE 4: variable-semantic-recovery          │
│ Recover meaningful names and types for       │
│ local variables                              │
│ Gate: generic v1..vN or scalar-in-ptr-ctx    │
└─────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────┐
│ PHASE 5: control-flow-normalizer             │
│ Normalize goto/label spaghetti into          │
│ structured control flow                      │
│ Gate: goto/label patterns detected           │
└─────────────────────────────────────────────┘
  │
  ▼
DONE: compilable, semantically restored .c + .o
```

---

## CRITICAL: File Integrity — NEVER Truncate the Target File

**This is the most common and most catastrophic failure mode in automated code repair.** A sub-agent reads a portion of a large file (e.g., via `Read` with offset/limit), makes modifications, then uses `Write` to save — but `Write` **overwrites the entire file** with only the content the agent had in context. The unwritten portion is permanently destroyed.

### Mandatory File Integrity Protocol (EVERY phase MUST follow)

1. **Record file size BEFORE spawning the sub-agent**: `wc -l <file>.c` and `wc -c <file>.c`
2. **Sub-agent prompts MUST include**: "You MUST read the ENTIRE target file before making any modifications. Never use Write on a file you have not fully read. Prefer Edit for targeted changes. After all modifications, verify the file is non-empty and complete."
3. **After sub-agent completes, BEFORE any compilation**: Verify file integrity:
   ```bash
   # Check file is non-empty
   test -s <file>.c || { echo "FATAL: file is empty!"; cp <file>.c.bak.phase<N> <file>.c; exit 1; }
   # Check line count hasn't dropped by more than 20% (allowance for code removal)
   ```
4. **If file is empty or severely truncated** → IMMEDIATELY restore from backup. Report: "Phase N: FILE TRUNCATION DETECTED — restored from backup. Sub-agent failed to preserve file integrity."
5. **After restoration** → Re-run the phase scan. If still needed, re-spawn with stronger file-integrity instructions.

### Why This Happens

The `Write` tool overwrites the entire file. If an agent reads only lines 1-200 of a 5000-line file, makes edits, then calls `Write` with only those 200 lines, lines 201-5000 are destroyed. The `Edit` tool is safe (it does string replacement), but `Write` is an overwrite bomb when the agent hasn't read the full file. **Every sub-agent prompt must warn about this.**

---

## How to Execute — IMPORTANT

**This pipeline runs by invoking each sub-skill via dedicated sub-agents.** Do NOT inline the sub-skill logic yourself. For each phase, spawn a sub-agent that loads the corresponding skill and executes it independently. This keeps each sub-skill's full methodology, rules, and context isolated, preventing context bloat and avoiding cross-phase contamination.

### Execution Pattern (per phase)

```
1. SCAN    — Check the gate conditions against the current .c file.
2. DECIDE  — If gate matches → proceed. If not → skip and report.
3. BACKUP  — Save a copy of the current .c file (cp <file>.c <file>.c.bak.phase<N>).
             This is the rollback point if the phase exceeds the retry limit.
4. MEASURE — Record pre-spawn file metrics (line count, byte count) for integrity check.
5. SPAWN   — Use Agent(subagent_type="general-purpose") with a prompt that tells it
             to invoke Skill(skill="<skill-name>") on the target file.
             The sub-agent loads the skill, executes it, and reports results back.
             CRITICAL: The prompt MUST include file-integrity instructions (see below).
6. INTEGRITY CHECK — Verify file is non-empty and size is reasonable.
   └─ EMPTY or >50% truncated → RESTORE from backup. Skip phase. Report truncation.
7. VERIFY  — Recompile with strict gcc.
   ├─ PASS → Delete backup. Proceed to next phase.
   └─ FAIL → Enter fix loop (max 3 attempts):
       ├─ Attempt 1-3: Diagnose error, fix minimally (cast/decl), recompile.
       │   └─ PASS → Proceed to next phase.
       └─ ALL 3 FAILED → ROLLBACK: cp <file>.c.bak.phase<N> <file>.c, delete backup.
           Report: "Phase N ROLLED BACK after 3 failed fix attempts. Reason: <why>."
           Continue to next phase. Do NOT block the pipeline.
```

### Why Rollback Instead of Blind "Fix Forward"

The old "fix forward, never revert" rule creates a cascade failure: if Phase 2 hallucinates a wrong struct that happens to compile (because casts paper over the mismatch), Phases 3-5 are forced to build on garbage — adding increasingly absurd casts just to keep the compiler quiet. A 3-strike rollback prevents this. The rolled-back phase is skipped, and the pipeline continues with the pre-phase code intact, so later phases operate on real (un-hallucinated) data.

### Sub-Agent Invocation (Preferred)

For each phase whose gate passes, spawn a sub-agent with the `Skill` tool embedded in its prompt.

**Every sub-agent prompt MUST start with this file-integrity preamble** (this is non-negotiable — it prevents the #1 cause of file destruction):

```
FILE INTEGRITY: You MUST read the ENTIRE target file before any modification.
Never use Write on a file unless you have read every line. Prefer Edit for
targeted changes. After all modifications, verify the file is non-empty.
```

| Phase | Sub-agent prompt (summary) | Gate |
|-------|---------------------------|------|
| 1 | `[FILE INTEGRITY PREAMBLE] Use Skill(skill="decompile-repair") to make <file>.c compilable with strict gcc. Read the full file first, then fix compilation errors using Edit (not Write). Report: number of errors fixed, list of modifications, and confirm .o file produced.` | Always runs |
| 2 | `[FILE INTEGRITY PREAMBLE] Use Skill(skill="restore-decompiled-structs") to scan and restore degraded struct types in <file>.c. Read the full file first. Use Edit for PAMA replacements. Do NOT use Write — the file is large and Write will truncate it. Report: number of structs defined, PAMA expressions replaced, and compilation status. If no degradation detected, report skip reason.` | PAMA / degradation patterns detected |
| 3 | `[FILE INTEGRITY PREAMBLE] Use Skill(skill="restore-function-signatures") to restore degraded function signatures in <file>.c. Read the full file first. Prefer Edit for signature changes. Report: return types, param types, param names, calling conventions, and dropped parameters restored. If no degradation detected, report skip reason.` | Generic signatures / degraded calls |
| 4 | `[FILE INTEGRITY PREAMBLE] Use Skill(skill="variable-semantic-recovery") to recover meaningful variable names and types in <file>.c. Read the full file first. Use Edit for targeted renames. Report: names recovered, types recovered, skipped (ambiguous). If no generic names or type degradation, report skip reason.` | Generic v1..vN / scalar-in-pointer-context |
| 5 | `[FILE INTEGRITY PREAMBLE] Use Skill(skill="control-flow-normalizer") to normalize goto/label patterns in <file>.c into structured control flow. Read the full file first. Use Edit for targeted restructuring. Report: loops, switches, if-else, short-circuit, ternary recovered, gotos preserved. If no goto/label patterns, report skip reason.` | Goto/label patterns detected |

### Fallback: Direct Skill Invocation

If sub-agents are unavailable, fall back to invoking each skill directly in the main context via `Skill(skill="<skill-name>")`. Be aware this may cause context accumulation across phases — after each phase completes, summarize the result concisely before proceeding to the next.

---

## Phase 1: Compilation Repair

**Skill**: `decompile-repair`

**Always runs** — this is the pipeline entry point.

Make the decompiled `.c` file compilable via iterative compile → diagnose → patch → recompile. Fix missing `#include` headers, implicit declarations, type mismatches, and missing return statements.

**Compile command** (exact):

```bash
gcc -c -Werror=implicit-function-declaration -Werror=implicit-int \
    -Werror=incompatible-pointer-types -Werror=int-conversion \
    -Werror=return-type -fno-builtin -fmax-errors=0 -I.
```

**Gate**: Always proceed. This phase MUST produce a `.o` file with zero errors before the pipeline continues.

> Do NOT perform struct restoration, signature recovery, or semantic recovery during this phase. Fix only compilation errors.

---

## Phase 2: Struct & Type Restoration [FOUNDATION]

**Skill**: `restore-decompiled-structs`

**This phase is the foundation for all subsequent phases.** 数据决定逻辑 — data determines logic. Phase 2 eliminates PAMA (pointer-arithmetic member access) and recovers global/local complex data types. Phase 3 (signatures), Phase 4 (variable semantics), and Phase 5 (control flow) all depend on correct type information from this phase. A hallucinated struct here poisons everything downstream.

### Phase 2 Hard Constraints

1. **Every struct field MUST be backed by a concrete PAMA pattern** in the code. If you cannot point to the exact `*((T *)ptr + N)` expression that justifies a field, do NOT create that field.
2. **Every type inference MUST cite evidence** — which usage site, which API call, which comparison proves the type.
3. **Preserve original when uncertain.** If confidence is below MEDIUM, keep the original PAMA expression. A preserved PAMA is better than a wrong field.
4. **Check compilation after EVERY struct definition.** Define one struct, verify it compiles, then proceed to the next. This localizes errors — if a struct breaks compilation, you know exactly which one.
5. **If a struct causes compilation errors that cannot be fixed in 3 attempts**, revert that specific struct's definition and keep the original PAMA code. Add a `// Skipped: struct restoration failed — insufficient evidence` comment.

### Gate — Run when any of the following are detected in the compilable file:

| # | Trigger Pattern | Signal |
|---|----------------|--------|
| 1 | `*((_QWORD *)ptr + N)` / `*((_DWORD *)ptr + N)` / `*((_WORD *)ptr + N)` / `*((_BYTE *)ptr + N)` | IDA PAMA |
| 2 | `*((int *)ptr + N)` / `*((long long *)ptr + N)` / `*((short *)ptr + N)` / `*((char *)ptr + N)` | Standard C PAMA |
| 3 | `*((void **)ptr + N)` / `*((char **)ptr + N)` | Nested pointer PAMA |
| 4 | `*(_DWORD *)(base + offset)` / `*(int *)((char *)base + offset)` | Offset-based access |
| 5 | `void *` variable used with pointer-arithmetic member access | Generic pointer degradation |
| 6 | `_UNKNOWN *` type | Unknown type degradation |
| 7 | Stack array heterogeneous usage: same `_QWORD arr[N]` used as byte buffer AND pointer storage | Struct flattened to array |

**Quick scan**:

```bash
grep -nE '\*\(\((_QWORD|_DWORD|_WORD|_BYTE|int|unsigned int|long|unsigned long|long long|unsigned long long|short|unsigned short|char|unsigned char|void|float|double|size_t|ssize_t|ptrdiff_t|intptr_t|uintptr_t|u?int\d+_t)\s*\*\s*\)' file.c
```

**If no patterns**: Skip this phase.

---

## Phase 3: Function Signature Restoration

**Skill**: `restore-function-signatures`

**Gate — Run when any of the following are detected:**

| # | Trigger Pattern | Dimension |
|---|----------------|-----------|
| 1 | Return type is `__int64` / `unsigned __int64` / `__int32` | D1: Generic return |
| 2 | Parameter type is `__int64` / `_QWORD` / `unsigned __int64` / `int` used as pointer | D2: Generic param types |
| 3 | Parameter named `a1`-`aN` or `v1`-`vN` (decompiler defaults) | D3: Generic param names |
| 4 | `__fastcall`/`__stdcall`/`__cdecl` conflict with target ABI or call sites | D4: Calling convention |
| 5 | `((ret_type (*)())func)()` — inline cast strips arguments from call | D5: Dropped parameters |

**If no degradations**: Skip this phase.

---

## Phase 4: Variable Semantic Recovery

**Skill**: `variable-semantic-recovery`

**Gate — Run when any of the following are detected:**

| # | Trigger Pattern | Signal |
|---|----------------|--------|
| 1 | Local variables named `v1`-`vN` | Generic decompiler names |
| 2 | `_DWORD` / `_QWORD` / `_BYTE` / `_WORD` locals used in pointer contexts (passed to `strlen`/`malloc`/`free`, null-checked, dereferenced) | Scalar in pointer role |

**If no generic names or type degradation**: Skip this phase.

---

## Phase 5: Control Flow Normalization

**Skill**: `control-flow-normalizer`

**Gate — Run when any of the following are detected:**

| # | Trigger Pattern | Signal |
|---|----------------|--------|
| 1 | Backward `goto` to a label above | Loop candidate |
| 2 | Forward `goto` targeting loop header/exit from within body | `continue` / `break` candidate |
| 3 | Chain of `if (v == const) goto CASE_N` | `switch-case` candidate |
| 4 | Consecutive `if (!A) goto SAME; if (!B) goto SAME;` | Short-circuit AND/OR |
| 5 | Both branches of if-else assign to the same variable | Ternary candidate |
| 6 | Sequential labels with interleaved gotos, no structured loops | Flattened CFG |

**If no goto/label patterns**: Skip this phase.

---

## Execution Rules

1. **Fixed order**: Phases MUST run 1 → 2 → 3 → 4 → 5. Each phase depends on the output of prior phases.
2. **Gated execution**: Phase 1 always runs. Phases 2-5 run only when their trigger conditions are met. Scan the entire file before deciding.
3. **Phase 2 is the foundation (数据决定逻辑)**: Phase 2 eliminates PAMA and recovers complex data types. All subsequent phases depend on correct type information from Phase 2. Phase 2 MUST be strictly data-driven — every struct field, every type inference MUST be backed by concrete PAMA patterns in the code. Never hallucinate fields or types. If uncertain, preserve the original PAMA.
4. **Backup before each phase**: Before spawning the sub-agent for any phase, save a backup of the current `.c` file. This is the rollback point.
5. **File integrity check after each phase**: After the sub-agent returns, verify the file is non-empty and has not lost more than 20% of its lines before proceeding to compilation. If the file is empty or severely truncated, restore from backup immediately and report the failure. This is the #1 failure mode — do not skip this check.
6. **Compilation verification**: Phase 1 must produce a clean `.o`. Phases 2-5 must re-verify compilation with the same strict gcc command after each phase's changes.
7. **Retry limit — 3 attempts max**: If a phase introduces compilation errors, fix minimally (casts, declarations). Retry up to 3 times. Do NOT add increasingly creative casts to force compilation — if the same type error persists across 2 attempts, the underlying restoration is likely wrong.
8. **Rollback on retry exhaustion**: If all 3 fix attempts fail, ROLLBACK the phase: restore the backup file, delete the backup, report the rollback reason, and SKIP to the next phase. Do NOT let a broken phase block the pipeline. A skipped phase is better than a hallucinated foundation.
9. **Memory semantics**: All phases follow the Supreme Rule. Preserve original memory read/write paths, dereference levels, and vulnerability trigger paths.
10. **Non-destructive**: Each phase only adds type information, semantic names, and structure. Original logic and data flow are preserved.

---

## Final Report

After all phases complete, produce a summarized report:

```
=== Repair Full Pipeline Complete ===

Phase 1 (decompile-repair):
  - Errors fixed: N
  - .o file: <path>
  - [ROLLED BACK after 3 failed fix attempts. Reason: <why>] or <status>

Phase 2 (restore-decompiled-structs):
  - [SKIPPED: no degradation signatures detected] or
  - Structs defined: N / PAMA expressions replaced: N
  - [ROLLED BACK after 3 failed fix attempts. Reason: <why>] or <status>

Phase 3 (restore-function-signatures):
  - [SKIPPED: no signature degradation detected] or
  - Return types: N / Param types: N / Param names: N /
    Conventions: N / Dropped params: N
  - [ROLLED BACK after 3 failed fix attempts. Reason: <why>] or <status>

Phase 4 (variable-semantic-recovery):
  - [SKIPPED: no generic variables or type degradation] or
  - Names recovered: N / Types recovered: N
  - [ROLLED BACK after 3 failed fix attempts. Reason: <why>] or <status>

Phase 5 (control-flow-normalizer):
  - [SKIPPED: no goto/label patterns detected] or
  - Loops: N / Switches: N / If-else: N /
    Short-circuit: N / Ternary: N
  - [ROLLED BACK after 3 failed fix attempts. Reason: <why>] or <status>

Rollbacks: N phase(s) rolled back (pipeline continued without them)
Compilation verified: yes
Final artifacts: <file>.c, <file>.o
```

