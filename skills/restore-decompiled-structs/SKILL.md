---
name: restore-decompiled-structs
description: Restore degraded struct/class types and eliminate raw pointer-arithmetic member access in decompiled C/C++ code (IDA Pro, Hex-Rays, ghidra, angr). Triggered when encountering *((_QWORD*)ptr + N), *((int*)ptr + N), *((DWORD*)base + offset), *((void**)ptr + N), void* generic pointers, _UNKNOWN types used as structs, or hardcoded byte-offset member access patterns.
version: 1.2.0
---

# Restore Decompiled Structs & Complex Types

Repair structural degradation in decompiled C pseudocode (IDA Pro / Hex-Rays / ghidra / angr). Recover struct/class definitions, replace raw pointer-arithmetic member access with named field access, and infer correct types for variables that have degenerated to `void*`, `_QWORD`, `_DWORD`, `__int64`, or `_UNKNOWN`.

---

## CRITICAL: Memory Semantics Above All Syntax Rules

**This section is the supreme rule. It overrides ALL other repair rules when there is a conflict.**

Decompiled code has pervasive type loss — variables that were originally pointers are often recognized as `_QWORD`, `int`, `unsigned __int64`, or other scalar types by the decompiler. Your task is to restore the **underlying memory logic**, NOT to tamper with that logic to eliminate compiler warnings. All repairs must preserve the original memory read/write paths, dereference levels, and data-flow edges — no adding `&` to "fix" a type mismatch, no changing pointer dereference depth, no altering the physical stack or heap access patterns.

---

## CRITICAL: File Integrity — NEVER Truncate the Target File

**This is the SINGLE MOST COMMON catastrophic failure in automated code repair.** The `Write` tool overwrites the ENTIRE file with whatever content you provide. If you read only part of a large file, make edits, then call `Write` with only those lines, the unread portion is permanently destroyed — the file gets truncated to a fraction of its original size.

### Mandatory Rules (Non-Negotiable)

1. **Read the ENTIRE file before any Write.** You MUST use the `Read` tool without any offset or limit parameter to read the complete file before you are permitted to use `Write`. If the file is too large to read at once, you must use `Edit` exclusively — `Write` is forbidden.

2. **Prefer `Edit` over `Write` for ALL changes.** Struct definitions, PAMA replacements, variable type changes — all of these should be done with `Edit` (targeted string replacement). `Write` is an overwrite bomb when you haven't read the full file. Use it ONLY as an absolute last resort.

3. **After EVERY modification, verify file integrity.**
   ```bash
   # Check file is non-empty
   test -s <file>.c || echo "FATAL: FILE IS EMPTY!"
   # Check line count is reasonable
   wc -l <file>.c
   ```

4. **If you detect truncation:** Stop immediately. Report the issue. Do NOT attempt to "fix forward" from a truncated file. The pipeline maintains a backup — let it handle the rollback.

### Self-Check Before Every Write

Before calling `Write` on any file, you MUST answer YES to ALL:
- [ ] Did I read the ENTIRE file with `Read` (no offset, no limit)?
- [ ] Is the content I'm writing the COMPLETE file (not just what I modified)?
- [ ] Could I accomplish this with `Edit` instead? (If YES → use Edit, not Write)

If you cannot answer YES to all of these, use `Edit`.

---

## Core Objective

**Given** decompiled C code where `dest->message = "ok"` has degraded to `*((_QWORD *)dest + 2) = (__int64)"ok"` (or `*((char **)dest + 1) = "ok"` in standard-C-emitting decompilers), **restore** proper struct definitions, member names, and typed access so the code is semantically readable and compilable.

---

## Fundamental Principle: 数据决定逻辑 (Data Determines Logic)

**This skill is the foundation for all subsequent pipeline phases.** PAMA elimination and complex type recovery provide the type context that function signature restoration, variable semantic recovery, and control flow normalization all depend on. A hallucinated struct here poisons everything downstream.

### Anti-Hallucination Rules

1. **Every field MUST be backed by a concrete PAMA pattern.** Before creating any struct field, identify the exact `*((T *)ptr + N)` expression that proves it exists. If you cannot point to the source line, do NOT create the field.
2. **Every type inference MUST cite evidence.** The `// Restored:` comment for each field must reference the specific usage site(s) that justify its type.
3. **Preserve original when uncertain.** If confidence is below MEDIUM (per Rule 5's heuristics table), keep the original PAMA expression. A preserved PAMA is better than a hallucinated field.
4. **Incremental verification.** Define one struct at a time, verify compilation, then proceed to the next struct. This localizes errors — if a struct breaks compilation, you know exactly which one.

**Why this matters**: If this phase hallucinates a struct that happens to compile (because C casts paper over type mismatches), the pipeline's "fix forward" rule would force Phases 3-5 to build on top of garbage — adding increasingly absurd casts just to satisfy the compiler. Correctness here is the gatekeeper for the entire pipeline.

---

## Pattern Recognition: When to Trigger

Activate this skill when decompiled code exhibits **any** of the following degradation signatures:

### Category A — Pointer-Arithmetic Member Access (PAMA)

**IDA / Hex-Rays custom types:**
```
*((_QWORD  *)ptr + N)   // QWORD-indexed member access
*((_DWORD  *)ptr + N)   // DWORD-indexed member access
*((_WORD   *)ptr + N)   // WORD-indexed member access
*((_BYTE   *)ptr + N)   // BYTE-indexed member access
```

**Standard C types (ghidra / angr / other decompilers):**
```
*((long long          *)ptr + N)   // 8-byte member access (same as _QWORD)
*((unsigned long long *)ptr + N)   // 8-byte member access
*((int                *)ptr + N)   // 4-byte member access (same as _DWORD)
*((unsigned int       *)ptr + N)   // 4-byte member access
*((long               *)ptr + N)   // 4 or 8-byte (platform-dependent)
*((unsigned long      *)ptr + N)   // 4 or 8-byte (platform-dependent)
*((short              *)ptr + N)   // 2-byte member access (same as _WORD)
*((unsigned short     *)ptr + N)   // 2-byte member access
*((char               *)ptr + N)   // 1-byte member access (same as _BYTE)
*((unsigned char      *)ptr + N)   // 1-byte member access
```

**Pointer-to-pointer (nested pointer) PAMA — easily miscomputed:**
```
*((void **)ptr + N)   // pointer array / vtable / pointer field access
*((char **)ptr + N)   // string array or char* field access
*((int  **)ptr + N)   // int-pointer array or int* field access
```

**Offset-based access (both IDA and standard types):**
```
*((_DWORD *)(base + offset))      // DWORD at explicit byte offset from base
*((int *)((char *)base + offset)) // int at explicit byte offset from base
*((long long *)((char *)base + offset))  // 8-byte field at explicit offset
```

**Key insight**: `(T *)ptr + N` means the Nth `sizeof(T)` offset, NOT byte offset N.

### Category B — Generic/Void Pointer Casts Used as Structs
```c
void *ctx;
*((_DWORD *)ctx + 3) = some_value;     // ctx is really a struct*
free(*(void **)ctx);                // ctx->some_pointer_field
memcpy((char *)ctx + 8, src, len);      // writing to ctx->field at offset 8
```

### Category C — _UNKNOWN / Sized-Type Overlays
```c
_UNKNOWN *g_state;                       // really a typed global pointer
*(_DWORD *)(g_state + 4 * index) = val;  // struct or array access pattern
```

### Category D — Mixed Access Sizes to Same Base
```c
*(_DWORD *)(base + 0)  = a;   // int field at offset 0
*(_QWORD *)(base + 8)  = b;   // pointer/long field at offset 8
*(_WORD  *)(base + 16) = c;   // short field at offset 16
*(_BYTE  *)(base + 18) = d;   // char field at offset 18
```

### Category E — Implicit Struct in malloc / sizeof
```c
ctx = malloc(40);                     // allocates 40 bytes => likely a struct
memcpy(dest, src, sizeof(SomeType));      // sizeof used as struct size hint
```

### Category F — Function Pointers Called via Offset
```c
(__call *)(*((_QWORD *)vtable + 3))(obj, args);  // vtable call degraded
```

### Category G — Stack Array Heterogeneous Degradation (Struct Flattened to Array)

When a decompiler flattens a struct into a wide-scalar local array (e.g., `_QWORD dest[5]`), the same variable exhibits **heterogeneous usage** — it's treated as a byte buffer at the base but as pointers/objects at specific indices. This "split personality" is the key signal that the variable is a degraded struct.

```c
_QWORD dest[5];
strcpy((char *)dest, src);           // used as byte buffer
HIBYTE(dest[1]) = 0;                 // byte manipulation on specific index (null-terminator)
printLine((const char *)(dest[2]));  // specific index cast to pointer type
```

**Heterogeneous Usage Clues (Must exist on the SAME variable):**
- The base is used as a byte buffer: `strcpy((char *)dest, ...)`, `memcpy(dest, ...)`
- Specific indices are cast to pointers: `(const char *)(dest[2])`, `(void *)(dest[3])`
- Specific indices undergo byte manipulation to act as string terminators: `HIBYTE(dest[1]) = 0;` (sets the 16th byte, proving a 16-byte char array boundary)
- Byte-level macros for bitwise math (`HIBYTE(...) ^=`, `&=`, `|=`) are NOT heterogeneous usage — these are crypto/network endianness operations and should be IGNORED.

---

## Strict Rules & Mathematical Constraints (MUST FOLLOW)

### Rule 1: POINTER-ARITHMETIC BYTE-OFFSET LAW (Non-Negotiable)

**In C, `((T *)ptr + N)` advances by `N * sizeof(T)` bytes, NOT N bytes.**

| Expression | sizeof(T) | Byte Offset | Correct Mental Model |
|---|---|---|---|
| `*((_QWORD *)ptr + 0)` | 8 | 0x00 | member at offset 0 |
| `*((_QWORD *)ptr + 1)` | 8 | 0x08 | member at offset 8 |
| `*((_QWORD *)ptr + 2)` | 8 | 0x10 | member at offset 16 |
| `*((_QWORD *)ptr + 3)` | 8 | 0x18 | member at offset 24 |
| `*((_DWORD *)ptr + 0)` | 4 | 0x00 | member at offset 0 |
| `*((_DWORD *)ptr + 1)` | 4 | 0x04 | member at offset 4 |
| `*((_DWORD *)ptr + 2)` | 4 | 0x08 | member at offset 8 |
| `*((_DWORD *)ptr + 3)` | 4 | 0x0C | member at offset 12 |
| `*((_WORD *)ptr + 1)` | 2 | 0x02 | member at offset 2 |
| `*((_BYTE *)ptr + 7)` | 1 | 0x07 | member at offset 7 |
| `*((int *)ptr + 5)` | 4 | 0x14 (20) | member at offset 20 |
| `*((char *)ptr + 12)` | 1 | 0x0C (12) | member at offset 12 |
| `*((long long *)ptr + 1)` | 8 | 0x08 | member at offset 8 |
| `*((char **)ptr + 2)` | **8** (pointer) | **0x10 (16)** | **member at offset 16, NOT 2!** |
| `*((void **)ptr + 3)` | **8** (pointer) | **0x18 (24)** | **member at offset 24, NOT 3!** |
| `*((int **)ptr + 1)` | **8** (pointer) | **0x08** | **member at offset 8, NOT 4!** |

**MANDATORY**: Before converting ANY pointer-arithmetic expression, you MUST:
1. Identify the cast type `T` in `(T *)ptr + N`
2. Compute `byte_offset = N * sizeof(T)`
3. Use this byte offset to reason about what struct member lives at that byte offset

**FATAL ERROR TO AVOID**: Treating `*((_QWORD *)ptr + 2)` as "offset 2 bytes." This is WRONG. It is offset `2 * 8 = 16 (0x10)` bytes.

**FATAL ERROR TO AVOID — Nested pointer sizeof trap**: When `T` is itself a pointer type (e.g., `char **`, `void **`, `int **`, `long **`, `uint8_t **`), `sizeof(T)` equals the **machine pointer size** (8 bytes on 64-bit, 4 bytes on 32-bit), NOT the size of the ultimate pointee type.
- `*((char **)ptr + 2)` → `sizeof(char *)` = 8 (on 64-bit), so byte offset = 2 × 8 = **16**, NOT 2 × 1 = 2.
- `*((void **)ptr + 1)` → `sizeof(void *)` = 8 (on 64-bit), so byte offset = 1 × 8 = **8**, NOT undefined.
- `*((int **)ptr + 3)` → `sizeof(int *)` = 8 (on 64-bit), so byte offset = 3 × 8 = **24**, NOT 3 × 4 = 12.
- **Rule of thumb**: If `T` contains a `*` (is a pointer type), then `sizeof(T) == sizeof(void *)`. Always verify before computing the byte offset.

### Rule 2: MIXED-TYPE OFFSET NORMALIZATION

When different cast types access the same base, normalize all to byte offsets before comparison:

```c
*((_QWORD *)ctx + 1) = ptr;       // byte offset = 1 * 8 = 8  => field at offset 8
*((_DWORD *)ctx + 2) = count;     // byte offset = 2 * 4 = 8  => SAME field at offset 8!
```

```c
*((long long *)ctx + 1) = val;    // byte offset = 1 * 8 = 8  => field at offset 8
*((int *)ctx + 2) = count;        // byte offset = 2 * 4 = 8  => SAME field at offset 8!
```

If two accesses land at the same byte offset but with different types, the struct field's true type must accommodate both uses (likely a union or the larger type).

### Rule 3: ARRAY-STYLE INDEXED ACCESS

`*((_DWORD *)(base + 4 * index))` is NOT a struct member — it is `base[index]` where `base` is a `_DWORD*` (or `int*`).
`*((int *)(base + 4 * index))` — same pattern, same conclusion: `((int *)base)[index]`.

Pattern: `*((T *)(base_addr + constant_multiplier * variable_index))`
- If `constant_multiplier == sizeof(T)`, this is `((T *)base)[index]`
- If the base is a known array, use `array[index]` syntax directly

### Rule 4: ALIGNMENT-AWARE PADDING INFERENCE

Struct member layout follows platform ABI rules. On x86/x86_64 (System V / cdecl):
- **Natural alignment**: each member aligned to its own size (e.g., `int` at 4-byte boundary, `double` at 8-byte boundary, `short` at 2-byte boundary)
- **Struct alignment**: struct size padded to alignment of its largest member
- **Between-member padding**: implicit gap bytes may exist

**Padding inference algorithm**:
1. Collect all observed byte offsets for a given base pointer
2. Sort offsets ascending
3. For each offset, compute the size of the member that ends there (from the write type)
4. If `next_offset > current_offset + current_member_size`, insert padding: `char __pad_N[M]` where `M = next_offset - (current_offset + current_member_size)`
5. The struct's total size = `max(observed_offset + sizeof(type_at_that_offset))`, padded to alignment

**Example**:
```
Observed: offset 0 (DWORD), offset 12 (QWORD)
=> offset 0: DWORD (4 bytes), ends at byte 4
=> gap: 12 - 4 = 8 bytes padding
=> offset 12: QWORD (8 bytes), ends at byte 20
=> struct size >= 20, but QWORD alignment => padded to 24
```

### Rule 5: TYPE INFERENCE HEURISTICS (Context-Based, No Guessing)

Infer struct member types from usage context. Apply these heuristics in order; the first match wins.

| # | Context Clue | Inferred Type | Confidence |
|---|---|---|---|
| 1 | Result is passed to `free()` / `free()` | `void *` (pointer, allocated memory) | HIGH |
| 2 | Result is assigned from `malloc()` / `malloc()` / `calloc()` | `void *` or specific pointer type | HIGH |
| 3 | Value compared against NULL (0) and used as address | pointer type (determine pointee from other uses) | HIGH |
| 4 | Value passed to `strcpy()`, `strcat()`, `strlen()`, `strcmp()` | `char *` | HIGH |
| 5 | Value passed to `memcpy()`, `memset()`, `memmove()` as dest | `void *` or `char *` | MEDIUM |
| 6 | Value used in integer arithmetic (add, mul, bitwise) | `int` / `unsigned int` (check signedness from branch context) | HIGH |
| 7 | Value passed to `isalpha()`, `isdigit()`, etc. | `char` or `int` | HIGH |
| 8 | Value used as loop counter / array index | `int` or `unsigned int` | HIGH |
| 9 | Value compared with `<`, `>`, `<=`, `>=` against another int | same type as the compared value | MEDIUM |
| 10| Value written via `*((_BYTE *)ptr + N)` or `*((char *)ptr + N)` | `char` or `unsigned char` | MEDIUM |
| 11| Value written via `*((_WORD *)ptr + N)` or `*((short *)ptr + N)` | `short` or `unsigned short` | MEDIUM |
| 12| Value written via `*((_DWORD *)ptr + N)` or `*((int *)ptr + N)` | `int` or `unsigned int` | MEDIUM |
| 13| Value written via `*((_QWORD *)ptr + N)` or `*((long long *)ptr + N)` | 8-byte type (pointer, `long long`, `double`) — use context | MEDIUM |
| 14| Function pointer called through offset | deduce signature from call site arguments | MEDIUM |
| 15| Value ANDed with bitmask like `& 0xFF` | `unsigned char` (byte extraction) | LOW |
| 16| Value declared but never initialized/used | can't infer — keep as `_UNKNOWN` | NONE |

**CRITICAL**: If you CANNOT determine the type with HIGH or MEDIUM confidence, preserve the original hex type. Do NOT guess. It is better to keep `_DWORD field_x` than to incorrectly assert `int field_x`.

### Rule 6: RECURSIVE STRUCT POINTER UNWRAPPING

When you see:
```c
void *a1;
*(_DWORD *)(*(_QWORD *)a1 + 8) = val;
```
Or with standard C types:
```c
void *a1;
*(int *)(*(void **)a1 + 8) = val;
```

This means `a1` is a pointer to a struct that contains a pointer at offset 0, which points to another struct with an int at offset 8. 

**Express as**: `a1->nested->field = val`

Work from outside in: unwrap the outer pointer access first, then recurse into the inner dereference.

### Rule 7: VTABLE / FUNCTION POINTER TABLE RECOGNITION

```c
(__call *)(*((_QWORD *)vtable + N))(obj, ...);
// or
(__call *)(*((void **)vtable + N))(obj, ...);
```

This is a virtual method call: `vtable[N](obj, ...)` or `obj->vtable[N](obj, ...)`.

If `vtable` is at offset 0 of the struct, it may be the C++ vtable pointer (typically named `__vftable` or `_vptr`).

### Rule 8: MINIMAL CHANGE PRINCIPLE

- **DO**: Replace pointer arithmetic with named member access, define struct types, add forward declarations
- **DO**: Add `// Restored:` comments explaining each struct/type recovery decision
- **DO NOT**: Change program logic, control flow, or data flow semantics
- **DO NOT**: Remove or comment out code blocks
- **DO NOT**: Change function signatures unless the parameter type was clearly degraded (e.g., `void *` where usage proves it's `FILE *`)
- **DO NOT**: Invent field names you're not confident about — use `field_XX` where XX is the hex byte offset

### Rule 9: ARRAY-TO-STRUCT UNFLATTENING

When a wide-scalar local array (e.g., `_QWORD dest[5]`) exhibits **heterogeneous usage** on the SAME variable, it MUST be converted back to a struct. Do NOT judge by a single line — look at whether the same variable shows "split personality" across its full context.

**Heterogeneous Usage Clues (Must exist on the SAME variable):**
- The base is used as a byte buffer: `strcpy((char *)dest, ...)`
- Specific indices are cast to pointers: `(const char *)(dest[2])`
- Specific indices undergo byte manipulation to act as string terminators: `HIBYTE(dest[1]) = 0;` (This explicitly sets the 16th byte, proving a 16-byte char array boundary)

**Conversion Algorithm for _QWORD dest[5]:**
- Offset 0-15: `dest[0]` to `dest[1]` used for string → `char field_0[16];`
- Offset 16-23: `dest[2]` cast to pointer → `void * field_16;`
- Offset 24-39: `dest[3], dest[4]` (remaining) → `char field_24[16];` (or padding)

**Refactoring Code:**
- Change declaration: `struct struct_dest { char f0[16]; void *f16; void *f24; } dest;`
- Change `strcpy((char *)dest)` to `strcpy(dest.f0)`
- Change `HIBYTE(dest[1]) = 0` to `dest.f0[15] = 0`
- Change `dest[2]` to `dest.f16`

### Rule 10: RETRY LIMIT — REVERT ON PERSISTENT FAILURE

If a struct restoration introduces compilation errors that cannot be fixed after **3 attempts**:

1. **Revert that specific struct** — remove the struct definition, restore all original PAMA expressions for that struct's base pointer.
2. **Add a skip comment**: `// Skipped: struct restoration failed for <base_var> — insufficient evidence to infer correct layout.`
3. **Do NOT add increasingly creative casts** to force compilation. If the same type mismatch persists across 2 fix attempts, the struct layout inference is likely wrong.
4. **Continue with remaining structs.** One failed struct does not block restoration of other structs in the same file.

This prevents the cascade failure described in the Fundamental Principle — a wrong struct that "compiles" via casts would poison all downstream phases.

---

### Analysis Steps (Internal Reasoning)

When restoring struct types, work through these analysis steps internally:

1. **Identify Base Pointers** — Find all variables used as base pointers in PAMA patterns
2. **Collect and Normalize All Accesses** — For each base pointer, compute byte offsets using `N * sizeof(T)`
3. **Detect Overlaps and Consolidate** — Check for same-byte-offset accesses with different cast types
4. **Infer Struct Layout** — Sort by byte offset, determine field types and sizes, insert padding for gaps
5. **Infer Struct Size** — Largest (offset + size) rounded up to alignment boundary; cross-validate with malloc size
6. **Generate Named Fields** — Create the `typedef struct { ... }` definition
7. **Map to Replacement** — Convert each PAMA expression to named member access

---

## Complete Example: Input → Analysis → Output

### Example Input (Decompiled Degraded Code)

```c
//----- (000013A0) --------------------------------------------------------
int __cdecl process_message(void *conn, const char *msg, int len)
{
  void *dest; // [esp+10h] [ebp-8h]

  if ( !conn || !msg )
    return -1;

  // Allocate message buffer
  dest = malloc(24);
  if ( !dest )
    return -2;

  // Copy connection info
  *(_QWORD *)dest = *(_QWORD *)conn;          // ???
  *(_DWORD *)((char *)dest + 8) = len;         // ???

  // Copy message string
  *((_QWORD *)dest + 2) = (__int64)strdup(msg);
  if ( !*((_QWORD *)dest + 2) )
  {
    free(dest);
    return -3;
  }

  // Set status
  *(_DWORD *)((char *)dest + 20) = 1;          // ???

  // Log it
  *((_DWORD *)dest + 3) = *(_DWORD *)(conn + 8);  // ???
  printf("processed msg id=%d\n", *((_DWORD *)dest + 3));

  // Send to handler
  send_to_handler(dest);
  return 0;
}
```

### Analysis Walkthrough

```
## Struct Restoration Analysis

### Step 1 — Identify Base Pointers
- Variable `conn`:  `*(_QWORD *)conn` at offset 0, `*(_DWORD *)(conn + 8)` at offset 8
- Variable `dest`:  allocated 24 bytes, multiple accesses

### Step 2 — Normalize All Accesses to `dest`

| Base | Cast Type       | Index/Offset      | sizeof(T) | Byte Offset | Operation     | Value Type Hint      |
|------|-----------------|-------------------|-----------|-------------|---------------|----------------------|
| dest | _QWORD *        | +0                | 8         | 0x00        | store         | copied from conn[0]  |
| dest | char* + 8       | explicit          | 1         | 0x08        | store _DWORD | len (int)            |
| dest | _QWORD *        | +2                | 8         | 0x10        | store         | strdup result (char*)|
| dest | _QWORD *        | +2                | 8         | 0x10        | load + branch | pointer              |
| dest | char* + 20      | explicit          | 1         | 0x14        | store 1       | int/flag             |
| dest | _DWORD *        | +3                | 4         | 0x0C        | store         | copied from conn+8   |

CHECK: _DWORD *dest + 3 => 3 * 4 = 12 (0x0C). But we have char*+8 at 0x08 (DWORD=4 bytes ends 0x0C). Wait —
*(_DWORD *)((char *)dest + 8) is at byte offset 8, size 4, spans [8, 12).
*(_DWORD *)dest + 3 is at byte offset 12 (3*4=12).
These are ADJACENT, not overlapping. Good.

Also: char*+20 at 0x14, size 4 (store _DWORD value 1), spans [20, 24).
malloc(24) => struct size 24. Consistent!

### Step 3 — Detect Overlaps and Consolidate

Offset 0x00: only _QWORD access => single 8-byte field
Offset 0x08: (_DWORD *)((char *)dest + 8) => DWORD at byte 8
Offset 0x0C: (_DWORD *)dest + 3 = 3*4=12 => DWORD at byte 12
Offset 0x10: (_QWORD *)dest + 2 = 2*8=16 => QWORD at byte 16 => pointer (strdup result)
Offset 0x14: (_DWORD *)((char *)dest + 20) => DWORD at byte 20

Layout:
 0x00-0x07: QWORD (8 bytes) — connection data pointer/cookie
 0x08-0x0B: DWORD (4 bytes) — message length
 0x0C-0x0F: DWORD (4 bytes) — message ID (copied from conn+8)
 0x10-0x17: QWORD (8 bytes) — strdup'd message (char*)
 0x14-0x17: DWORD (4 bytes) — status flag
 0x14+4=0x18=24 => matches malloc(24)

### Step 4 — Infer Struct Layout

Offset 0x00: QWORD. Context: copied from *conn. conn is a void* that also has data at offset 8.
  This is likely a pointer-sized field. Type: probably a pointer or ID.
  Semantic: seems like connection identifier. Name proposal: `conn_data` or `peer_id`.

Offset 0x08: DWORD. Value is `len` parameter => message length.
  Type: `int` or `unsigned int`. Name: `msg_len`.

Offset 0x0C: DWORD. Value copied from conn+8, printed as "msg id=%d".
  Type: `int`. Name: `msg_id`.

Offset 0x10: QWORD. Stores result of strdup() => char*.
  Also checked against NULL. Type: `char *`. Name: `message`.

Offset 0x14: DWORD. Stores constant 1. Semantic: status/flag.
  Type: `int`. Name: `status`.

No gaps between fields! All adjacent. Total size = 24, matches malloc(24).

### Step 5 — Struct Name

Based on usage (holds message metadata + payload pointer), name it `message_buf_t`.

### Step 6 — Proposed Struct

typedef struct {
    void   *conn_data;   // offset 0x00, 8 bytes — opaque connection pointer
    int     msg_len;     // offset 0x08, 4 bytes — length of message
    int     msg_id;      // offset 0x0C, 4 bytes — message identifier
    char   *message;     // offset 0x10, 8 bytes — duplicated message string
    int     status;      // offset 0x14, 4 bytes — processing status flag
} message_buf_t;
// sizeof(message_buf_t) = 24, matches malloc(24)

### Step 7 — Replacement Map

| Original Code                                    | Replacement                   |
|--------------------------------------------------|-------------------------------|
| *(_QWORD *)dest = *(_QWORD *)conn;               | dest->conn_data = *(void **)conn |
| *(_DWORD *)((char *)dest + 8) = len;             | dest->msg_len = len;          |
| *((_QWORD *)dest + 2) = strdup(msg);         | dest->message = strdup(msg)|
| *((_DWORD *)dest + 3) = *(_DWORD *)(conn + 8);   | dest->msg_id = *(int *)(conn + 8)|
| *(_DWORD *)((char *)dest + 20) = 1;              | dest->status = 1;             |
```

### Example Output (Repaired Code)

```c
// Restored: struct definition inferred from member access patterns
typedef struct {
    void   *conn_data;   // offset 0x00: copied from *conn
    int     msg_len;     // offset 0x08: assigned from `len` parameter
    int     msg_id;      // offset 0x0C: copied from conn+8, printed as %d
    char   *message;     // offset 0x10: result of strdup, null-checked
    int     status;      // offset 0x14: set to 1 as processing flag
} message_buf_t;

//----- (000013A0) --------------------------------------------------------
// Restored: parameter `conn` is likely a pointer to a struct containing
// a pointer at offset 0 and an int at offset 8.
int __cdecl process_message(void *conn, const char *msg, int len)
{
  message_buf_t *dest; // [esp+10h] [ebp-8h]

  if ( !conn || !msg )
    return -1;

  dest = (message_buf_t *)malloc(sizeof(message_buf_t));
  if ( !dest )
    return -2;

  dest->conn_data = *(void **)conn;
  dest->msg_len = len;

  dest->message = strdup(msg);
  if ( !dest->message )
  {
    free(dest);
    return -3;
  }

  dest->status = 1;

  dest->msg_id = *(int *)((char *)conn + 8);
  printf("processed msg id=%d\n", dest->msg_id);

  send_to_handler(dest);
  return 0;
}
```

---

## Special Cases & Edge Patterns

### Case: Inline Struct Without Typedef (Nested in Function)

When a struct is used only within one function, define it as a local `struct { ... }` or keep it as a file-scope typedef. Prefer file-scope typedef for reuse across functions.

### Case: Partial Struct (Only Some Members Observed)

If only offsets 0, 8, and 24 are accessed but malloc suggests 40 bytes:
- Define only observed fields at their offsets with padding for unobserved regions
- Add comment: `// Unobserved fields at offsets 16-23 (8 bytes padding)`
- Use `char __unobserved_N[gap_size]` for gaps

### Case: Union Detection

When the same byte offset is accessed with two different sizes, consider a union:
```c
// offset 0x00 accessed as both DWORD and QWORD → union
union {
    struct { int lo; int hi; };
    long long full;
} field_00;
```

### Case: Bitfield Detection

When values are masked and shifted before use (e.g., `(*(_DWORD *)ptr >> 3) & 0x1F`), the field is likely a bitfield:
```c
unsigned int flag : 5;   // bits 3-7 of the DWORD at this offset
```

### Case: Cross-Function Consistency

If the same struct is accessed in multiple functions, consolidate into a single shared typedef placed before the first function that uses it. The struct definition must be consistent across all uses.

---

## Output Format

After analysis, output the repaired code with:
1. New `typedef struct { ... }` definitions placed before the first function that uses them
2. `// Restored:` comments on every added struct definition explaining the inference
3. `// Restored:` comments on converted member accesses
4. Updated variable declarations (change `void *` to the inferred struct pointer type)
5. All original logic preserved — only the type system and access patterns changed

---

## Verification Checklist

After restoration, verify:

- [ ] Every `*((TYPE *)base + N)` expression has been converted to named member access (both IDA custom types and standard C types)
- [ ] All byte-offset calculations are verified correct (N * sizeof(TYPE)), **with special attention to nested pointer types where sizeof(T) = pointer size, not pointee size**
- [ ] Struct layout has no overlapping fields (unless union)
- [ ] Padding is inserted where offsets have gaps
- [ ] Total struct size matches malloc allocation size (when available)
- [ ] Type inferences are justified by context (not guessed)
- [ ] Code compiles with ` gcc -c -Werror=implicit-function-declaration -Werror=implicit-int -Werror=incompatible-pointer-types -Werror=int-conversion -Werror=return-type -fno-builtin -fmax-errors=0 -I.`
- [ ] Original control flow is unchanged

