# Dante source language and toolchain

Ghostbusters: TVG Remastered ships its level logic as compiled `.dante` text modules for a custom 16-bit stack VM. The original `.dante` *sources* were never shipped. This document describes the toolchain that lets you write new logic anyway:
```
   .dn source  --dante compile-->  .s assembly  --dante asm-->  .dante  --dante link-->  patched .dante
        ^            \____________________ dante compile -o *.dante ____________________/
        |                                                              |
        \-------------------------- dante decompile --------------------------/
```

Everything is proven against the shipped corpus:

| proof | command | result |
|---|---|---|
| the model can regenerate a shipped module | `dante roundtrip <dir>` | 21/21 byte-identical |
| the assembler can regenerate it from text | `dante roundtrip-asm <dir>` | 21/21 byte-identical |
| the compiler reproduces shipped functions | `tests/test_reproduction.py` | 8/8 word-identical |
| the linker relocates a whole module | `tests/test_link.py` | all jumps/exports preserved, `verify` OK |
| the decompiler structures the corpus | `dante decompile <dir> -o <outdir>` | 98.4% structured, 1.6% labelled `goto`, 0 raw |
| decompile -> recompile -> compare words | `tests/test_decompile_roundtrip.py` | **93.1% word-identical**, 99.6% recompilable, 21/21 `__init` identical |
| every module rebuilt from its own decompilation | `tests/test_whole_module.py` | 21/21 compile, 0 exports lost, 0 new call-target changes |

---

## 1. Toolchain commands

### `dante` container commands

```
disasm  <f.dante> [--func NAME] [--raw]     annotated listing
disasm  <f.dante> --asm [-o out.s]          re-assemblable assembly source
asm     <f.s> -o <f.dante>                  assemble
verify  <f.dante|dir>                       structural verification
roundtrip / roundtrip-asm <f.dante|dir>     byte-identity proofs
link    <base.dante> --add <patch.s|.dante> [--hook "<target>=<callee>"] [--frame N] -o out
classid <Name> ...                          DYNCAST class id of a name
stats   <f.dante|dir>                       opcode histogram
```

### `dante decompile`

```
dante decompile <f.dante|dir> [-o outdir]   decompile to <stem>.dn
                [--func NAME]               print one export
                [--stdout]                  print instead of writing files
                [--compilable]              emit source `dante compile` accepts as-is
                [--symbols <symbols.json>]  extra native prototypes / enums / layouts
```

### `dante compile`

```
dante compile <src.dn> -o <out.dante>       compile a whole module
dante compile <src.dn> -S -o <out.s>        compile to assembly (for `dante link`)
              --no-init                     do not emit __<stem>_init() (for link patches)
              --module <stem>               module stem (default: the source file name)
              --lib <world/global.dante>    make that module's C exports callable
              --symbols <symbols.json>      extra native prototypes / enums / struct layouts
```

`dante/data/dante_symbols.json` (505 native prototypes and 74 struct-layout assumptions named by the 21 shipped modules' FIXUPS/ASSUMPTIONS, plus 85 enum constants recovered from the engine's own registration calls) is loaded automatically, as is `dante/data/dante_api.json` for the DYNCAST class table.

### `dante vm`

```
dante vm call <f.dante> "<proto>" [args...] --lib g.dante --world w.py    run one export
dante vm run  <f.dante> --entry "<proto>" --ticks N --lib g.dante --world w.py  tick the scheduler
dante vm natives <f.dante>                                               list natives it calls
```
Headless interpreter for the script VM (`tests/test_vm.py` exercises it). Every native is answered by a "world": `--world w.py` loads a Python file exposing `make_world()` (or `WORLD`), a `dict` of prototype -> `callable(vm, this, *params)`. An unstubbed native is logged (`vm.world.unknown`) and answered 0/""/null/zero-Vector, never a fault. `dante.vm.world`'s `GameWorld` is the reference shim (actor groups, characters, spawning, HUD/sfx/checkpoint bookkeeping).

---

## 2. The source language (`.dn`)

A C-like language whose type spellings are exactly the ones the VM's prototype strings use, so `int absInt(int)` in your source *is* the export string in the compiled module.

### Types

| spelling | meaning | size (script units) |
|---|---|---|
| `void int float bool String` | primitives | 4 (`void` = 0) |
| `Vector` | 3 floats | 12 |
| `@CFoo` | object **reference** (a pointer the engine hands you) | 4 |
| `CFoo` | object **value** (an engine-owned global, e.g. `CActorGroup g`) | 4 |
| `SFoo` | struct value (`SSpawnInfo`, `SDamageInfo`, ...) | `sizeof` from the symbol DB |
| `@SFoo` | pointer to a struct (how natives take struct parameters) | 4 |
| `@@SFoo` | pointer to such a pointer (`void spawn(@@SSpawnInfo)` handlers) | 4 |
| `@float`, `@int` | pointer to a primitive (e.g. the engine global `gTimeSlice`) | 4 |
| `EFoo` | enum | 4 |
| `(ret(argtypes))` | function value, e.g. `(void(@CCharacter))` | 4 |

The difference between `@CFoo` and `CFoo` is load-bearing: a method call on a *value* object passes `&object` (`LEA`), on a *reference* it passes the reference (`MOV`). This is what the shipped compiler does for `g_FlierKitchSmall.add(...)` versus `flyer.setVictimUponSpawn(...)`.

### Declarations

```c
module cemetery3;                              // module stem -> __cemetery3_init/_data

native "void myNewNative(String,float)";       // a native the symbol DB does not know
native "void CGhostbuster::forceDeploySuperTrap{@CGhostbuster}(@CWayPoint)";
                                               // method form needs Class:: and the {@Class}
                                               // self annotation, like the entries in
                                               // dante/data/dante_symbols.json
extern "void wait(float)";                     // a script function in another module
struct SThing { String name; float t; }        // struct layout, if not in the symbol DB
enum EMyEnum { eThingA, eThingB }              // enum constants, if not in the symbol DB

extern @CGhostbuster hero;                     // a global owned by the engine/another module
extern CActorGroup g_Flyers;                   //   (D fixup only, no D export)
int  gCount = 0;                               // this module's global: D export + init
String gName = "immortal";                     //   Strings get STRNEW + STRCPY in the init
Vector gOrigin;

void myFunc(@CSpawn sp) { ... }                // C export "void myFunc(@CSpawn)"
bool  isReady();                               // forward declaration
```

You only need `native` for something the symbol DB doesn't already carry. The DB holds the 505 natives the shipped modules actually call, which is a subset of the engine's full API. Anything outside that set has to be declared by hand.

A trailing parameter may carry a default, written into the prototype string. This works on `native`, on `extern`, and on a function you define yourself:
```c
native "void shakeCamera(float,float,float=0.0)";
extern "void wait(float,bool=false)";
void spawnAt(@CSpawn s, String cit, String variant = "standard") { }
```
A call that omits the parameter gets the default written into the argument slot at the call site, so `shakeCamera(0.1, 4.0)` still fills all three. The annotation is stripped from the fixup text, which stays `{N:"void shakeCamera(float,float,float)"}` so the runtime still resolves it. Accepted literals are numbers, `true`, `false`, `null` and strings. An enum constant is not accepted. Ten of the 505 shipped prototypes carry defaults, `int beginThread((void()),bool=false)` among them.

Every non-`extern` global is emitted as a `D` export and initialised in `void __<stem>_init()`, which the compiler appends as the module's last export (as the shipped compiler does). Pass `--no-init` when the compiled functions will be *linked into* a module that already has one.

### Statements

`{ }` blocks, `if`/`else`, `while`, `do { } while (cond);`, `for(init; cond; step)`, `for (;;)`, `return [expr];`, `break`, `continue`, declarations, assignments, compound assignments (`x += e`, `-=`, `*=`, `/=`, `%=`, where the lvalue is evaluated once, which matters for `*p -= f`), `x++` and `x--` in both prefix and postfix form (statement-only sugar for `x += 1`), expression statements, `IDENT:` labels with `goto IDENT;`, and:
```c
thread worker(actor, 2.0);      // THREAD: start `worker` on a new VM thread
try {                           // TRY -> handler ... ENDTRY: the engine unwinds to the
    playStreamingCinemat(c);    // handler when the thread is aborted (a skipped cinematic)
} catch {
    stopStreamingCinemat(c);
}
```

```c
    if (actor.isEnabled()) goto L_853;      // one conditional jump, as shipped
    goto done;                              // an unconditional one
L_853:
    ...
done:
```

A label is **function-scoped** (not block-scoped): it may be defined in any block of the function and jumped to from any other, and a `goto` may name a label defined later (forward references are resolved at the end of the function). A duplicate label, or a `goto` to one that is never defined, is a compile error. `goto` is a raw jump: it runs no `STRFREE`/`DTORPOP` for the scopes it leaves, so do not jump out of a scope that owns a `String` or a registered struct. It exists so that `the decompiler`'s labelled rendering of irreducible control flow recompiles. Hand-written `.dn` rarely needs it.

`thread` compiles to the shipped idiom: `THREAD tmp, {C:"<proto>"}, #<argbytes>` followed by `MOV [tmpStack]+n, <arg>` for each argument. `try`/`catch` is `TRY -> catch; body; ENDTRY #4; goto end; catch: handler; end:` (53 sites in the corpus, all cinematic sequences).

### Expressions

* literals: `12`, `-1`, `0x1F`, `2.5`, `-2.5`, `2f`, `true`, `false`, `null`, `"text"`
  (`\\`, `\n`, `\"`); a negative literal is a single immediate
* `a + b - c * d / e % f` for `int`/`float`; `+` on `String` is `STRCAT`;
  `Vector + Vector`, `Vector - Vector`, `Vector * float`, `Vector / float`
* comparisons `== != < <= > >=` (`>`/`>=` compile to swapped `LT`/`LE`, the VM has no `GT`)
* `&& || !` with short-circuit branches
* `obj.method(args)`, `func(args)`, `struct.member`, `vector.x/.y/.z`, `*ptr`, `&x`
  (address of a local, a struct, or a value-object global. `&x` is implicit wherever a value object is used as a `@CFoo`: arguments, comparisons, assignments)
* `c ? a : b`: both arms are stored straight into the destination (`return c ? a : b`
  is how the shipped `round()` is written)
* `(@CFoo) expr`: a `DYNCAST` with the class-name hash as the immediate
* a bare function name is a **function value** (`MOV L1C, {C:"void onDeath(@CCharacter)"}`)
* a bare enum-constant name, or `EHudMessage.eHudMessage_ObjectivesUpdated`
* conversions: `toInt(f)` `FTOI`, `toFloat(i)` `ITOF`, `toString(x)` `ITOS/FTOS/BTOS/VTOS`,
  `dot(a, b)` `DOT`

### What the language does **not** have

No `switch`, no arrays or indexing, no pointer arithmetic, no user-defined struct *values* beyond declaring an engine layout, no bitwise or unsigned operators (the VM has none), no user-defined operator overloading, no varargs, no recursion guard, and no type inference (`var`). Implicit conversion is limited to `int` literal → `float` in a float context and `int` → `float` (`ITOF`) when mixing in arithmetic or a comparison.

---

## 3. What the compiler emits, and why it matches

Everything below was read off the shipped corpus and is what makes the reproduction exact.

**Frame layout.** Locals are allocated from script offset 8 in declaration order and **released at scope exit** A later sibling scope reuses the slots. A loop body is an ordinary scope, freed before the loop condition, which is generated *after* the body. `ENTER n` is emitted as `n = max(32, highest_slot_base + 12)`.

**Temporaries.** Every statement starts its temporaries at `locals_end`, and within a statement:

1. *arithmetic* temporaries (results of `+ - * / %`, `STRCAT`, conversions, `DYNCAST`, `&&`/`||`/comparisons in value context, and String temps) are allocated **pre-order** (parent before children, left to right), and all of them come *before* any call block of the statement; 2. a comparison compiled to a branch **reserves a slot** that its own operands may reuse (`f - f1 < 0.5` puts the `SUBF` in the reserved slot; `i < g.N()` leaves it empty and the call block starts one slot higher); 3. **call blocks** and *materialisations* (`LEA` of a value object used as a reference, the `MOV Ln, *{D:@float g}` behind `*g`) follow with a stack discipline: a nested call's block starts **8 past** the enclosing block's end (the callee's frame header), sibling calls reuse the same base; 4. results are written **straight into their destination** whenever the VM allows it: a local, a call slot or the return slot receives `ADDI`/`ITOF`/`DYNCAST`/`LEA` directly. The compiler never copies a temporary into another temporary. Exceptions: `LEA` and `DYNCAST` into a *global* go through a temporary, and `!x` is `MOV d, x; EQ_B d, d, #0`.

The compiler implements this by generating each expression twice. A dry run measures the arithmetic temporaries, then the real run places the call blocks above them (`FuncGen.run_at`).

**Script → script calls.** Arguments are written ascending immediately below the callee's frame, the return slot(s) below the first argument; `CALL fn, frame=#<block end>` (the callee's `fp` is the caller's `fp + frame`).

**Script → native calls.** The block is `[ret][this][params...]` (`this` only for `Class::m{@Class}` prototypes, `ret` only for non-`void`); `args = #<block base>`, or `#-1` when the block is empty; `frame = #<block end>`.

**Strings.** Every `String` argument is materialised as a temporary in the parameter slot: `STRNEW` / `STRCPY` / `CALL` / `STRFREE`. `String` locals get a `STRNEW` at declaration and a `STRFREE` at scope exit. String literals are mode-5 operands whose value is the entry's `STRINGS` offset, patched by a `D` fixup against `__<stem>_data`.

**Struct locals.** A struct the engine registers (it has `void S::constructor{@S}()` / `destructor` natives in the symbol DB: `SSpawnInfo`, `SDamageInfo`, `SScriptWalkInfo`, `SMaterialVariable`, `SJointData`) is constructed and destroyed. A `struct` you declare in the source is plain storage and gets neither. For a registered one: `LEA slot, slot` (a self-pointer in the first slot) → `CALL {N:"void S::constructor{@S}()"}` → `CTOR slot, #0, {N:"void S::destructor{@S}()"}`, then `DTORPOP slot` at scope exit and before every `return`. Member accesses on a struct *local* are folded at compile time (`info.citFilename` becomes `L<base+4>`). Through an `@SFoo` *pointer* the offset is **baked** into the mode-7 word (`[A4]+40`, guarded by the `ASSUMPTIONS` line, with no fixup). A *class* member through an `@CFoo` reference is mode-7 `[Ln]+0` with a 16-bit `M` fixup. On a *global* it is a mode-5 operand carrying both a `D` and an `M` fixup at the same site (611 such sites in the corpus).

**Value-object globals** (`CActorGroup g;`) are constructed in `__<stem>_init()`: `LEA L8, *{D:g}` → `CALL constructor args=#8` → `CTOR *{D:g}, #0, {N:destructor}` (a mode-5 `CTOR` registers on the program-static destructor list); `@CFoo r = valueObject;` there is a direct `LEA *{D:r}, *{D:valueObject}`. All 21 shipped initialisers reproduce word-identically.

**Assumptions.** Every struct member offset and `sizeof` the compiler relies on is emitted as an `ASSUMPTIONS` line, so the loader re-checks it against the live engine tables ("Script is out of date and must be recompiled").

**Control flow.** `if (cond) goto L;` is a single conditional jump (the condition is *not* inverted around an unconditional `goto`, which is what the shipped code does too); `if/else` emits the condition as branches (`&&`/`||` short-circuit, comparisons are inverted rather than materialised), `for`/`while` emit `goto COND; BODY: ...; CONT: step; COND: cond -> BODY`; `for (;;)` is `BODY: ...; goto BODY` with no entry jump, and a function whose body cannot fall through gets no trailing `RET`. A `goto` whose target is the next instruction is removed by a fixed-point peephole, and a dead `goto` after a non-falling-through branch is never emitted. Both are needed to match the shipped output instruction-for-instruction. (`&&`/`||` reserve nothing. It is the *comparison* that reserves a slot, see *Temporaries*.)

**DYNCAST class ids.** The immediate is `classid(name)`, the hash the engine computes in the script-class descriptor constructor `FUN_1402D5320`:
```python
def classid(name):
    h = 0
    for ch in name:
        if ch.isalnum():                        # letters and digits only; '_' and punctuation skipped
            h = (h * 0x80 + ord(ch.lower()) * 0x20001 + (h >> 25)) & 0xFFFFFFFF
    return h            # emitted as a signed 32-bit immediate (hi word, lo word)
```

`CCharacter` → `0x75C2BB57` (1975696215), `CFlyerSmall` → `0xF8997C4E` (-124158898). It resolves **all 701** DYNCAST immediates in the corpus against `dante_api.json["classes"]` plus six names that table lacks (`CWayPoint`, `CEctoCarEffects`, `CPKESource`, `CCoal`, `CTraceEvidence`, `CPhantom`).

---

## 4. The assembly format (`.s`)

`dante disasm --asm` produces a complete, re-assemblable rendering; `dante asm` turns it back into a `.dante`. Round-tripping all 21 shipped modules through it is byte-identical, so the assembler is a faithful inverse of the disassembler (including layout, jump displacement computation, export offsets and the whole `FIXUPS` table).

```
.version 1
.datasize 500
.codesize 5124
.module global

.assume M "float Vector::x" 0
.assume S "sizeof(SSpawnInfo)" 12
.string 00000000 "Can't shuffle actor group..."
.global 000000E0 "Vector vzero"

.code
.func "int absInt(int)"
        ENTER   #32
        JLE_I   -> L_19, A4, #0
        MOV     A8, A4
        JEQ_I   -> L_1C, #0, #0
L_19:
        SUBI    A8, #0, A4
L_1C:
        RET
```

Operand syntax (a trailing `#0` operand may be omitted):

| form | mode | meaning |
|---|---|---|
| `#0` / `#1` | 0 / 1 | the VM's built-in 0 and 1 constants (no operand word) |
| `#<int>` | 2 | 16-bit immediate |
| `-> <label>` | 2 | jump; the assembler computes the displacement |
| `L<hex>` / `A<hex>` | 3 | stack slot (`A` = negative = the argument area) |
| `##<int>`, `##f:<float>`, `##class:<Name>` | 4 | 32-bit immediate / float bits / `classid()` |
| `*<int>` | 5 | 32-bit handle base (patched by a `D`/`N` fixup) |
| `L<hex>:32` | 6 | 32-bit stack slot (never emitted by the shipped compiler) |
| `[L<hex>]+<int>` | 7 | indirect `*(ptr at slot) + offset` |

Fixups are suffixes on the operand they patch: `{C:"proto"}`, `{D:"type name"}`, `{N:"proto"}`, `{I:"Enum value"}`, `{M:"type S::member"}` (32-bit, added into the operand's value, where a mode-5 operand may carry both a `D` and an `M`), and `{M16:"type S::member"}` for the 16-bit fixup on a mode-7 offset word.

Section ordering is regenerated, not copied: `ASSUMPTIONS` sorted case-insensitively by text, `STRINGS` by offset, `EXPORTS` as all `C` by offset then all `D` by offset, `FIXUPS` by symbol text case-insensitively with sites ascending. That rule was derived from the corpus and is what makes the round-trip byte-exact.

---

## 5. Patching an existing module (`dante link`)

```
dante compile mypatch.dn --no-init -S -o mypatch.s
dante link world/cemetery3.dante \
        --add mypatch.s --hook "void setupLevel()=void immortal_hello()" \
        -o out/cemetery3.dante
dante verify out/cemetery3.dante
```

`link` appends the patch's code, strings, globals and exports to the base module, then inserts `CALL {C:"<callee>"} frame=#8` immediately after the hooked function's `ENTER`, and re-lays-out the whole module.

**Why full relocation is safe here.** Nothing in the parsed model stores an absolute code address: jump displacements are turned back into references to the *instruction object* they target, `C` exports into references to their `ENTER`, fixup sites live on the operand words they patch, and call targets are prototype strings resolved by the loader. Re-running the layout recomputes every displacement, export offset and `FIXUPS` site. `tests/test_link.py` proves it by hooking a shipped module and checking that all 136 of its jumps still land on the same instructions, that no export is lost, and that `verify` passes. No trampoline is needed.

`--frame N` (default 8) is the injected call's frame offset. 8 is right for a call injected at the *top* of a function, where none of the caller's locals are live yet.

Notes / limits:

* A patch may not redefine an export the base module already has.
* New strings and globals are appended **after** the base module's data segment and
  `VM data size` grows accordingly. The base module's own string offsets never move.
* A patch compiled with a different `module` stem still works. `link` rewrites its
  `__<stem>_data` fixups to the base module's data symbol.
* Code addresses are 16-bit. The largest shipped module is 34k words, so there is room, but a
  patch that pushed a module past 65535 words would overflow (the engine reports "16-bit fixup overflow").

---

## 6. The decompiler (`dante.decompiler`)

`the decompiler` runs the pipeline backwards -- compiled `.dante` bytecode to `.dn` source:
```
dante decompile world/hotel1a.dante --func spawnFlierKitchSmall
dante decompile <corpus> -o scripts/                  # the whole corpus
```

What it recovers:

* **expressions** -- temp slots are forwarded into the expression that consumes them
  (single-use, dead-after-use, and never across a call unless the slot belongs to a call block); native/script call blocks (`[ret][this][params]` / `[ret][args]`) fold back into `obj.method(a, b)` / `f(a)`; `D` fixups become string literals or global names, `I` fixups enum constants, `C` fixups function values, `M`/`M16` fixups `s.member`, `DYNCAST` `(@CFoo) e`, mode-7 operands `*p`; `&&`/`||`/`!` come out of the short-circuit branch shapes and `>`/`>=` out of the VM's swapped `LT`/`LE`.
* **control flow** -- `if`/`else`, `while`, `for` (the `init; goto COND; TOP: body; step;
  COND: cond -> TOP` shape), `break`, `continue`, `return`, `thread f(a, b)`, plus `for (;;)` for a bare backward jump and `if (c) break;` for a conditional loop exit. Where the graph is irreducible it degrades to `L_<hex>:` labels and `goto` (1.6% of the corpus) and says so in a comment; that rendering **recompiles** -- `.dn` has labels and `goto`. A `TRY`/`ENDTRY` inside such a body is folded back into `try { } catch { }` (its shipped shape is `TRY -> catch; body; ENDTRY; goto end; catch: ...; end:`). A body whose exception scope does *not* fit that shape is flagged, never emitted with the scope silently dropped.
* **types** -- parameters and the return type come from the export prototype. Locals are
  inferred from the arithmetic family, native prototype parameter types, `this` receivers, `DYNCAST` targets, member and global fixup texts, and pointer dereferences.
* **declarations** -- locals are declared at their first assignment (which is where the
  shipped compiler allocates them), struct locals keep their `SSpawnInfo info;` form, a declaration referenced outside its block is hoisted, and a slot that was allocated but never read is reconstructed as `int _unusedNN;` so later slot offsets line up.
* **module shape** -- `module <stem>;`, `native`/`extern`/`enum`/`struct` declarations for
  everything the module's own `FIXUPS`/`ASSUMPTIONS` name, then this module's globals with their `__<stem>_init()` values folded in (`String kLevelNameHotel1a = "Hotel1a";`), then the functions in export order, each with a `// @<hex offset>` comment.

What it hides: `ENTER`, `STRNEW`/`STRFREE`, `CTOR`/`DTORPOP`, the struct self-pointer `LEA`, the `&&`/`||` reserved slot, and every temporary.

### Proof

`tests/test_decompile_roundtrip.py` decompiles each shipped module, recompiles the result with `the compiler` and compares the instruction words (and their fixups) function by function:

| | share of the 5,132 exports |
|---|--:|
| fully structured (no `goto`) | 98.4% |
| labelled-`goto` fallback | 1.6% (81, all of which recompile; 42 word-identical) |
| raw-disassembly fallback | 0% |
| recompiles **word-identical** | **93.1%** (4,780) |
| recompiles (words differ, slot numbering) | 6.5% (332) |
| stubbed (the compiler rejects the decompiled source) | 0.4% (20) |

`tests/test_whole_module.py` goes one step further: it rebuilds each module from its own `--compilable` dump and runs `dante compare` on the result. All 21 compile, no export is lost, and no export's call graph changes except the 24 differences ledgered in that script (the compiler's method-overload fallback picking `CCarEffects::wakeUp` where the module calls `CPhysicsObjectBase::wakeUp`, and loop rotations that move a call within a body). Only one of those 24 is in a labelled-`goto` body.

`dante decompile <corpus> -o scripts/` writes one `.dn` per module and a per-module table of how much of each was structured.

### Known limits

Everything below still compiles and runs. What's missing is word-identity with the shipped build, not correctness.

**Temporary-slot numbering** accounts for most of the remaining 6.9%. When a slot is reused across sibling scopes the decompiler sometimes reads a temporary as a local, or the reverse, and the recompiled function ends up with the same opcodes in different slots.

A few shipped shapes also aren't modelled yet: two `ENDTRY #2` sites, `waitForFlag`'s `LEA` of a `@bool` parameter, and a call through a function-value global.

**Irreducible control flow** covers 81 functions, 1.6% of the corpus. It comes out as labelled `goto`. The compiler accepts that, so those bodies round-trip too, 42 of them word-identically.

A further 20 functions are rejected outright for a declaration-order or pointer-typing slip in the decompiled source, and are emitted as stubs. Four of those happen to be `goto` bodies.

**Method overloads** can resolve to the wrong class. The compiler matches `obj.m()` against the receiver's own class first. When that class doesn't declare `m`, it falls back to the first prototype in the symbol DB with that name and arity.

There's no class hierarchy in the symbol DB to do better, so a `@CCarEffects` receiver picks `CCarEffects::wakeUp` where the shipped module called `CPhysicsObjectBase::wakeUp`. That affects 10 exports in the corpus, all ledgered in `tests/test_whole_module.py`.

A `native "..."` declaration overrides the fallback for that name. It also outranks the prefix-match tie-break, so only declare one where you actually need it.

**Two smaller gaps.** A `(void(...))`-typed *local* can't be spelled in `.dn`, so it is emitted as `int` with the real type in a comment. And local variable names are invented throughout. The originals were never shipped.

## 7. Worked example

`tests/data/link_probe.dn` compiles one function and `tests/test_link.py` splices it into a shipped module, hooking it into an existing export -- the whole patch-a-module flow end to end.

`tests/data/global_lib.dn` and `tests/data/hotel1a_spawn.dn` are reproductions of shipped code. Comparing them against `dante disasm world/global.dante --func "int absInt(int)"` is the quickest way to see how a construct is meant to look.
