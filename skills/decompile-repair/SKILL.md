---
name: decompile-repair
description: Repair IDA Pro decompiled C pseudocode to make it compilable with strict gcc. Used when fixing decompiler output, fixing C compilation errors, repairing IDA decompiled code, or making decompiled C compile.
version: 2.0.0
---

# Decompiled C Code Repair

Repair IDA Pro decompiled C pseudocode so it compiles with `gcc` under strict declaration and typing rules. Operate in a tight loop: compile → diagnose → patch → recompile.

---

## CRITICAL: Memory Semantics Above All Syntax Rules

**This section is the supreme rule. It overrides ALL other repair rules when there is a conflict.**

Decompiled code has pervasive type loss — variables that were originally pointers are often recognized as `_QWORD`, `int`, `unsigned __int64`, or other scalar types by the decompiler. Your task is to restore the compilability of the **underlying memory logic**, NOT to tamper with that logic to eliminate compiler warnings.

### Rule 1: ABSOLUTELY FORBIDDEN — Address-of (`&`) to Fix Type Mismatches

When an **integer or wide-type scalar value** (e.g., `_QWORD dest[2]`, `unsigned __int64 val`, `int ptr`) is passed as a pointer argument to a function, producing a type mismatch error:

```
WRONG:  printLine((const char *)&dest[2]);
   This changes "read the VALUE from memory" into "read the STACK ADDRESS".
   It destroys the original memory semantics — if dest[2] contained
   attacker-controlled data from a buffer overflow, the vulnerability
   execution flow is completely eliminated.

RIGHT:  printLine((const char *)(dest[2]));
   This casts the VALUE itself to a pointer. The original memory read
   (dereferencing dest[2] as a scalar, then treating that scalar as an
   address) is preserved exactly. If dest[2] holds dirty data, printLine
   will attempt to dereference it -> crash / exploit trigger intact.
```

**Decision rule**: Before inserting any cast involving `&`, ask:
1. Does the original expression read a **value** from memory? (e.g., array subscript `arr[i]`, struct member `s->field`, dereference `*ptr`)
2. If YES → the cast MUST operate on the **value**, NEVER take its address.
3. Only use `&` when the original expression is already an address-taken operation (e.g., `&var`), which is rare in decompiled code.

**Memory layout verification for `&` traps**:

Original: `_QWORD dest[5];` at `[rbp-0x30]`. `dest[2]` is at `[rbp-0x20]` (byte offset 16 from buffer start).

```
WRONG: printLine((const char *)&dest[2]);
   -> Passes rbp-0x20 (a STACK ADDRESS, always valid, never crashes)
   -> Vulnerability: DESTROYED

RIGHT: printLine((const char *)(dest[2]));
   -> Reads 8 bytes from [rbp-0x20] as a QWORD value
   -> Casts that VALUE to a pointer
   -> Passes the (potentially attacker-controlled) value to printLine
   -> printLine dereferences it -> SEGFAULT at attacker-chosen address
   -> Vulnerability: PRESERVED
```

### Rule 2: Preserve Overflow and Crash Trigger Paths

Do NOT "fix" the following, even if they look suspicious:
- Array out-of-bounds access
- Buffer overflow (e.g., `strcpy` into a small buffer)
- Dereferencing attacker-controlled values as pointers
- Use-after-free patterns visible in the decompiled output
- Integer wraparound that feeds into size calculations

If the code demonstrates a vulnerability (e.g., dereferencing overwritten dirty data from a buffer overflow), **preserve its fatality**. The only exception is when the user explicitly asks you to fix a specific vulnerability.

### Rule 3: Only Do Explicitly Requested Modifications

When cleaning code for specific tools (e.g., CodeQL, SAST):
- Only perform **type casting** (`(char *)`, `(const char *)`, etc.) and **macro expansion**
- **Never** change any variable's physical read/write path
- **Never** add or remove pointer dereference levels
- **Never** introduce new variables to "fix" a type mismatch
- **Never** extract expressions into temporary variables. All casts MUST be done inline exactly where the original expression resides. Decomposition like splitting `func((char *)val)` into `char *tmp = &val; func(tmp);` destroys memory layout and is strictly forbidden

### Rule 4: Cast Syntax for Value-as-Pointer

When casting a scalar value to a pointer type, always parenthesize the value expression to make the intent clear:

```c
// Clear — the value is being cast:
printLine((const char *)(dest[2]));
void *ptr = (void *)(some_integer_value);

// These are also correct (array/pointer decay):
printLine((const char *)dest);       // dest is _QWORD[], decays to pointer
printLine((const char *)&dest[2]);   // ONLY when & was in the ORIGINAL code
```

---

## CRITICAL: File Integrity — NEVER Truncate the Target File

**This is the SINGLE MOST COMMON catastrophic failure in automated code repair.** The `Write` tool overwrites the ENTIRE file with whatever content you provide. If you read only part of a large file, make edits, then call `Write` with only those lines, the unread portion is permanently destroyed — the file gets truncated to a fraction of its original size.

### Mandatory Rules (Non-Negotiable)

1. **Read the ENTIRE file before any Write.** You MUST use the `Read` tool without offset/limit to read the complete file before you are permitted to use `Write`. If the file is too large to read at once, you must use `Edit` exclusively — never `Write`.

2. **Prefer `Edit` over `Write` for ALL changes.** The `Edit` tool does targeted string replacement and cannot truncate the file. `Write` should be your LAST resort, used ONLY when you have confirmed you have the full file content in context.

3. **After EVERY modification, verify file integrity.**
   ```bash
   test -s <file>.c || echo "FATAL: FILE IS EMPTY!"
   wc -l <file>.c  # sanity check line count
   ```

4. **If you realize you truncated the file:** The pipeline maintains a backup. Report the truncation immediately — do NOT attempt to "fix forward" from a truncated file.

### Self-Check Before Every Write

Before calling `Write` on any file, ask yourself:
- [ ] Did I read the ENTIRE file (all lines, no offset/limit)?
- [ ] Is the content I'm writing complete (not just what I changed)?
- [ ] Could I accomplish this with `Edit` instead?

If you answer NO to any of these, use `Edit`.

---

## Core Workflow

```text
1. Compile .c file with the exact command: 
   gcc -c -Werror=implicit-function-declaration -Werror=implicit-int -Werror=incompatible-pointer-types -Werror=int-conversion -Werror=return-type -fno-builtin -fmax-errors=0 -I.
2. Parse structured gcc errors (file, line, col, type, message, code). Ignore lines with 'warning:', focus ONLY on 'error:'.
3. Read relevant code slices around error lines.
4. Apply minimal patches (inject missing standard C #include headers at the top, add explicit type casts, add/delete/update lines).
5. Recompile to verify.
6. Repeat until compilation succeeds.
```

## Hard Constraints

1. **Memory semantics above all syntax rules** (see Supreme Rule section above). When a type mismatch involves a value that was read from memory, cast the VALUE, never take its address with `&`.
2. **Never change program logic** — no modifications to control flow, data flow, or semantics.
3. **Never add or remove function parameters** — signatures must be preserved.
4. **Never remove or comment out entire functions or blocks of logic.**
5. **Keep changes minimal** — fix only what is needed to compile.
6. **You MUST add missing `#include` headers** (e.g., `<stdlib.h>`, `<string.h>`) at the top of the file if `gcc` reports "implicit declaration of function" for standard functions like `malloc`, `exit`, `free`, `strcpy`, `memcpy`, etc.
7. **Address strict type errors with VALUE casts (NOT address-of casts)**: If gcc reports "incompatible pointer types" or "int-conversion" for an expression that reads a value from memory (array subscript, dereference, struct member access), cast the VALUE directly — e.g., `(const char *)(scalar_value)`. Only use `&`-cast if the original expression was ALREADY taking an address. DO NOT change variable declarations.
8. **You MAY add `extern` or global declarations** if needed (e.g., for undefined custom functions like `printLine`).
9. **Ensure function return boundaries**: If gcc reports a `return-type` error, ensure a proper `return` statement (e.g., `return 0;` or `return NULL;`) is added at the end of the function to maintain data flow integrity.
10. **Mark every modification** with a C comment: `// Modified: <reason>`.

### Mandatory Self-Check Before Every Cast Fix

Before applying any fix for "incompatible pointer types" or "int-conversion", you MUST internally determine the correct cast approach using this decision algorithm. You are NOT allowed to apply the patch until you have resolved this self-check:

**Decision algorithm for Q1:**

```
Q1: Does the argument expression read a VALUE from memory?
    YES — array subscript (arr[i]), pointer dereference (*ptr),
          struct member access (s.field, s->field),
          local variable that holds a computed value
    NO  — array/pointer decay (arr, ptr passed directly),
           string literal, function name
```

**Action mapping:**

```
If Q1 = YES -> use VALUE cast:  (TargetType)(expression)
If Q1 = NO  -> use pointer cast: (TargetType)expression
```

Examples:
```c
_QWORD dest[5];
printLine((const char *)(dest[2]));   // VALUE cast — dest[2] reads from memory

printLine((const char *)dest);        // pointer cast — array name decays to pointer

char buf[16];
printLine((const char *)buf);         // pointer cast — array name decays to pointer

int val;
printLine((const char *)(val));       // VALUE cast — reads the integer value stored in val
```

## Preferred Tool Chain

For each error, prefer: `compile → read code slice → patch apply`. Read targeted slices, apply targeted patches, do NOT dump entire files.

## Success Condition

Compilation succeeds when the exact following command produces a `.o` file with no errors:
`gcc -c -Werror=implicit-function-declaration -Werror=implicit-int -Werror=incompatible-pointer-types -Werror=int-conversion -Werror=return-type -fno-builtin -fmax-errors=0 -I.`
```
