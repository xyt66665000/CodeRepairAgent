---
name: variable-semantic-recovery
description: Recover meaningful variable names and correct types for decompiler-degraded local variables (v1→msg_len, _QWORD→void*). Uses API context, dataflow, and usage patterns with LLM semantic understanding. Triggers on "recover variable names", "fix variable types", "semantic recovery", "rename variables".
version: 1.0.0
---

# Variable Semantic Recovery

Recover semantic variable names and degraded pointer types in decompiled C pseudocode. When the decompiler assigns generic names (`v1`, `v2`, ...) and degrades `void *` / `char *` to `_DWORD` / `_QWORD`, restore both name and type by analyzing API usage, dataflow, and semantic context.

---

## CRITICAL: Memory Semantics Above All Syntax Rules

**This section is the supreme rule. It overrides ALL other repair rules when there is a conflict.**

Decompiled code has pervasive type loss — variables that were originally pointers are often recognized as `_QWORD`, `int`, `unsigned __int64`, or other scalar types by the decompiler. Your task is to restore the **underlying memory logic**, NOT to tamper with that logic to eliminate compiler warnings. All repairs must preserve the original memory read/write paths, dereference levels, and data-flow edges — no adding `&` to "fix" a type mismatch, no changing pointer dereference depth, no altering the physical stack or heap access patterns.

---

## CRITICAL: File Integrity — NEVER Truncate the Target File

**The `Write` tool overwrites the ENTIRE file.** If you read only part of a large file then use `Write`, the unread portion is destroyed. This is the #1 cause of catastrophic data loss in automated repair.

### Mandatory Rules

1. **Read the ENTIRE file before any Write.** Use `Read` without offset/limit. If the file is too large, use `Edit` exclusively.
2. **Prefer `Edit` over `Write` for ALL changes.** Variable renames, type changes — all targeted edits should use `Edit`. `Write` is a last resort.
3. **After every modification, verify:** `test -s <file>.c` (non-empty check), then `wc -l <file>.c` (sanity check line count).

---

## Core Principle

Leverage LLM knowledge of C standard library and common API signatures to infer **what a variable is** from **how it is used**. Every recovery must be justified by ≥1 specific usage site. When evidence is ambiguous, preserve the original and annotate — never guess.

---

## Analysis Framework

For each candidate variable, evaluate three axes. Apply recovery only when confidence is **HIGH**.

### Axis 1: Type Recovery — Is the declared scalar type actually a pointer?

A scalar-typed variable (`_DWORD`, `int`, `_QWORD`, `__int64`) is actually a **pointer** when ≥2 HIGH signals fire:

| # | Signal | Pattern | Confidence |
|---|--------|---------|------------|
| 1 | Assigned from allocation | `v = malloc/calloc/mmap(...)` | HIGH |
| 2 | Passed to deallocation | `free(v)` | HIGH |
| 3 | Null-pointer check with error/return path | `if (!v) return -1;`, `if (v == NULL) goto err;` | HIGH |
| 4 | Explicitly dereferenced | `*v = x`, `v->field` | HIGH |
| 5 | Cast to pointer type before use | `(char *)v`, `(const void *)v` | HIGH |
| 6 | Passed to function expecting pointer | `strlen(v)`, `memcpy(v, ...)`, `read(fd, v, n)` | HIGH |
| 7 | Assigned from another pointer-typed variable | `v = ptr` where `ptr` is `void *`/`char *` | HIGH |
| 8 | Used in pointer arithmetic | `v + offset`, `(char *)v + N` | MEDIUM |

**Decision**: ≥2 HIGH signals → recover to pointer type. Determine pointee type from Axis 3.

**ABI size constraint**: `_DWORD` (4 bytes) can only hold a pointer on 32-bit ABI. On 64-bit, only `_QWORD`/`__int64` (8 bytes) can hold a pointer. If `_DWORD` is used in pointer contexts on 64-bit code, the variable was likely a 32-bit handle/index masquerading as a pointer, or the decompiler truncated it — annotate and keep, do not retype.

### Axis 2: Name Recovery — What semantic role does this variable play?

Infer role from dataflow. Use LLM understanding of API semantics to select the best name.

| Role | Signals | Candidate Names |
|------|---------|-----------------|
| **Length / Size** | Assigned from `strlen()`, `sizeof()`; passed as size/count argument | `len`, `size`, `count`, `n`, `buf_len` |
| **Buffer / Destination** | Assigned from `malloc`; dest arg to `memcpy`/`strcpy`/`read`; freed | `buf`, `dest`, `data`, `payload` |
| **String** | Passed to `strlen`/`strcpy`/`strcmp`/`printf("%s",...)` | `str`, `message`, `name`, `path`, `input` |
| **Loop index** | `for (v = 0; v < limit; v++)`, used as array subscript | `i`, `j`, `k`, `idx`, `index` |
| **Result / Status** | Assigned from function return; compared against error codes; used in return | `result`, `ret`, `status`, `err`, `rc` |
| **File descriptor** | Used with `read`/`write`/`close`/`open`/`socket` as first arg | `fd`, `sockfd`, `sock` |
| **Opaque handle / Context** | Void* passed through multiple functions, stored in structs, freed at cleanup | `conn`, `ctx`, `handle`, `session` |
| **Counter / Position** | Incremented/decremented in loop, tracks position (not array index) | `count`, `pos`, `offset`, `cursor` |
| **Flag / Boolean** | Only assigned `0`/`1`/`true`/`false`; used in `if(v)` condition | `flag`, `found`, `done`, `ok`, `ready` |
| **Element value** | Read from array, passed to processing function | `val`, `item`, `elem`, `ch`, `byte` |

**Name selection priority**:
1. If the variable's purpose mirrors a named function parameter → derive from that parameter's semantic domain
2. If passed to a known API in a specific role → name for that role (e.g., `strlen(v)` → `v` is a string)
3. If multiple variables share the same role → disambiguate (`src_buf` / `dst_buf`, `buf1` / `buf2`)
4. Loop induction variables used only within a small loop body → single-letter (`i`, `j`, `k`) is acceptable
5. Variables spanning >10 lines or multiple blocks → descriptive multi-word name

### Axis 3: API Context — What does the function ecosystem tell us?

Use LLM knowledge of C standard library and common API signatures to determine precise types:

| API Call Pattern | Semantic | Type Implication |
|-----------------|----------|------------------|
| `v = strlen(s)` | `v` is string length | `v`: `size_t` or `int` |
| `strlen(v)` | `v` is a string | `v`: `const char *` |
| `strcpy(d, s)` | `d` = dest buffer, `s` = source string | `d`: `char *`, `s`: `const char *` |
| `memcpy(d, s, n)` | `n` = byte count | `n`: `size_t` or `int` |
| `v = malloc(n)` | `v` = allocated memory, `n` = size | `v`: `void *`, `n`: `size_t` |
| `free(v)` | `v` is a heap pointer | `v`: `void *` |
| `read(fd, buf, n)` | `fd` = file desc, `buf` = buffer, `n` = size | `fd`: `int`, `buf`: `void *`, `n`: `size_t` |
| `write(fd, buf, n)` | same as read | same |
| `v = open(p, f)` | `v` = file desc | `v`: `int` (fd) |
| `close(fd)` | `fd` = file desc | `fd`: `int` |
| `v = strdup(s)` | `v` = heap copy of `s` | `v`: `char *`, `s`: `const char *` |
| `printf(fmt, ...)` | `fmt` = format string | `fmt`: `const char *` |
| `v = atoi(s)` | `v` = parsed int, `s` = string | `v`: `int`, `s`: `const char *` |

**Reconciliation rule**: When a variable is used with multiple APIs expecting different types, the most specific/constrained type wins. Example: passed to both `memcpy(dest, v, n)` (expects `void *`) and `strlen(v)` (expects `const char *`) → `v` is `const char *`.

---

## Recovery Rules (Non-Negotiable)

### Rule 1: Confidence-Gated Application

Apply recovery only at HIGH confidence. If evidence supports multiple interpretations with similar likelihood, preserve the original and annotate:

```c
_QWORD v3; // Recovered: ambiguous — could be void* (from malloc) or size_t (used in arithmetic)
```

### Rule 2: Memory Semantics Preservation

A rename or retype MUST NOT change any memory read/write path, stack offset, or dereference level count.

- `int v1` → `int msg_len` — OK (same type, only name)
- `_QWORD v2` → `void *v2` — OK (same size on 64-bit)
- `_DWORD v3` → `char *v3` — OK **only on 32-bit** (`sizeof(_DWORD) == sizeof(char *) == 4`)
- `_DWORD v3` → `char *v3` — **FORBIDDEN on 64-bit** (`sizeof(_DWORD) == 4 ≠ 8 == sizeof(char *)`)

### Rule 3: Every Recovery Must Cite Evidence

Each recovery comment must cite ≥1 specific usage site:

```c
// Recovered: v2 → message (char *) — passed to strlen(@L42), assigned from strdup(@L38)
char *message;
```

If no usage line can be cited, skip the variable.

### Rule 4: Usage-Type Conflict Resolution

- All usage evidence agrees on a type ≠ declared type → recover
- Mixed usage evidence → keep declared type, annotate ambiguity
- Single ambiguous usage (e.g., only null-checked but never passed to typed API) → keep declared type

### Rule 5: Never Guess — Use Context Deliberately

Use LLM semantic understanding: if the surrounding code suggests a network server, prefer `conn`/`sockfd`; if file processing, prefer `fd`/`filename`. But when the code context provides no domain signal, do not invent one. Generic names (`buf`, `data`, `ptr`) are acceptable when domain context is absent.

### Rule 6: Mark All Changes

- Type change: `// Recovered: type _QWORD → void * — assigned from malloc(@L15), null-checked(@L16)`
- Name change: `// Recovered: v4 → msg_buf — dest buffer for memcpy(@L20), freed(@L30)`
- Combined: `// Recovered: v1 → msg_len (int), assigned from strlen(@L12)`

---

## Recovery Workflow

```
INPUT: compilable .c file with generic v\d+ names or degraded pointer types
  │
  ▼
SCAN — Identify candidates
  │  Local variables named v\d+
  │  _DWORD / _QWORD / _BYTE / _WORD variables in pointer-usage contexts
  │
  ▼
For EACH candidate:
  │
  ├─ COLLECT — All usage sites: assignments, comparisons, function calls, address-of
  │
  ├─ ANALYZE Axis 1 — Does usage contradict declared type?
  │    └─ MISMATCH? → Determine correct type from Axis 3 (API context)
  │
  ├─ ANALYZE Axis 2 — What semantic role?
  │    └─ ROLE FOUND? → Select best name from candidate table
  │
  ├─ DECIDE — Confidence check
  │    │  HIGH in both → APPLY (name + type)
  │    │  HIGH in type only → APPLY type, keep generic name, annotate
  │    │  HIGH in name only → APPLY name, keep declared type
  │    │  LOW in both → SKIP, annotate if ambiguous
  │    │
  │    └─ APPLY → Replace all occurrences, add // Recovered: comment
  │
  ▼
VERIFY — Recompile
  │  gcc -c -Werror=implicit-function-declaration -Werror=implicit-int \
  │      -Werror=incompatible-pointer-types -Werror=int-conversion \
  │      -Werror=return-type -fno-builtin -fmax-errors=0 -I.
  │
  │  New compile errors → fix forward (cast, extern). Do NOT revert recovery.
  │
  ▼
DONE
```

---

## Examples

### Example 1: Full Name + Type Recovery

**Before:**
```c
int process(int v1, int v2)
{
  int v3;
  char *v4;
  _QWORD v5;
  int v6;

  if (!v1 || !v2)
    return -1;
  v3 = strlen((const char *)v1);
  v4 = (char *)malloc(v3 + 1);
  if (!v4)
    return -2;
  memcpy(v4, (const void *)v1, v3);
  v4[v3] = 0;
  v5 = (_QWORD)strdup((const char *)v2);
  if (!v5) {
    free(v4);
    return -3;
  }
  v6 = send_message(v4, (const char *)v5);
  free(v4);
  free((void *)v5);
  return v6;
}
```

**After:**
```c
int process(const char *input, const char *extra)
{
  // Recovered: v3 → msg_len (int) — assigned from strlen(@L9), used for malloc/memcpy bounds
  int msg_len;
  // Recovered: v4 → msg_buf (char *) — allocated(@L10), filled via memcpy, freed(@L21)
  char *msg_buf;
  // Recovered: type _QWORD → char * — assigned from strdup(@L15), null-checked(@L16)
  // Recovered: v5 → extra_copy — heap copy of 'extra' parameter
  char *extra_copy;
  // Recovered: v6 → result (int) — return value from send_message(@L20)
  int result;

  if (!input || !extra)
    return -1;
  msg_len = strlen(input);
  msg_buf = (char *)malloc(msg_len + 1);
  if (!msg_buf)
    return -2;
  memcpy(msg_buf, input, msg_len);
  msg_buf[msg_len] = 0;
  extra_copy = strdup(extra);
  if (!extra_copy) {
    free(msg_buf);
    return -3;
  }
  result = send_message(msg_buf, extra_copy);
  free(msg_buf);
  free(extra_copy);
  return result;
}
```

### Example 2: Loop Index + Element Value

**Before:**
```c
int v1;
int v2;
for (v1 = 0; v1 < 10; v1++) {
    v2 = items[v1];
    if (v2 > 0)
        total += v2;
}
```

**After:**
```c
// Recovered: v1 → i (int) — loop induction variable, array index
int i;
// Recovered: v2 → val (int) — element read from items[], used in comparison/accumulation
int val;
for (i = 0; i < 10; i++) {
    val = items[i];
    if (val > 0)
        total += val;
}
```

### Example 3: Ambiguous — Skip with Annotation

**Before:**
```c
_QWORD v1;
v1 = some_opaque_func();
if (v1)
    do_thing(v1);
```

**After (no change — single ambiguous usage):**
```c
_QWORD v1; // Recovered: ambiguous — null-checked but no typed API usage to disambiguate pointer vs integer
v1 = some_opaque_func();
if (v1)
    do_thing(v1);
```

### Example 4: FD Recovery

**Before:**
```c
int v1;
char v2[256];
int v3;
v1 = open("/tmp/log", 0);
if (v1 < 0) return -1;
v3 = read(v1, v2, 255);
if (v3 > 0) v2[v3] = 0;
close(v1);
```

**After:**
```c
// Recovered: v1 → fd (int) — file descriptor from open(@L4), used with read/close
int fd;
char v2[256];
// Recovered: v3 → n (int) — byte count from read(@L6), used as string terminator
int n;
fd = open("/tmp/log", 0);
if (fd < 0) return -1;
n = read(fd, v2, 255);
if (n > 0) v2[n] = 0;
close(fd);
```

---

## Post-Recovery Compilation Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| `incompatible pointer types` after `_QWORD` → `void *` | Assignment from integer expression | Add explicit cast `(void *)(intptr_t)expr` — do NOT revert |
| Size mismatch: `_DWORD` → pointer on 64-bit | `_DWORD` is 4 bytes, 64-bit pointer is 8 bytes | Recover via `_QWORD` only on 64-bit; `_DWORD` cannot hold a 64-bit pointer |
| Duplicate name after rename | Two variables recovered to same name | Add suffix: `buf_1`, `buf_2` |
| `unused variable` warning after rename | Recovery preserved original declaration position | Suppress with `__attribute__((unused))` only if the original was also unused |

---

## Final Report Format

```
=== Variable Semantic Recovery Complete ===
- Variables analyzed: N
- Names recovered: N
- Types recovered: N
- Skipped (ambiguous): N
- Compilation verified: yes / no

Recoveries:
  v1 → msg_len (int)          // strlen result, bounds for malloc
  v4 → msg_buf (char *)        // allocated buffer, passed to memcpy
  v5 : type _QWORD → char *    // strdup result, null-checked, freed
  v5 → extra_copy              // heap copy of 'extra' parameter
  ...
```
