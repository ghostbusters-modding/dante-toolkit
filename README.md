# dante-toolkit

A homebrewed toolkit for the Dante scripting language used with Terminal Reality's Infernal Engine. Made for use in GBTVGR modding.

GBTVGR's level logic is stored as 21 compiled `world/*.dante` modules inside `COMMON.POD`, running on a custom VM (16-bit code words that the game interprets). This repo provides tools for reading, writing, building, and testing level scripts.

```
.dante  ──disasm──▶  .s  ──asm──▶  .dante           lossless, 21/21 byte-identical
.dante  ──decompile──▶  .dn  ──compile──▶  .dante   93.1% word-identical
  .dn   ──compile──▶  .dante  ──link──▶  patched module
.dante  ──▶  headless VM + fake world  ──▶  native-call trace
```

View [docs/dante_cookbook.md](docs/dante_cookbook.md) for common patterns used in the game scripts.
View [docs/dante_lang.md](docs/dante_lang.md) for the full language and toolchain reference.

## Install

Python 3.9+, no dependencies.

```bash
pip install -e .          # provides the `dante` command
pip install -e ".[test]"  # + pytest, to run the suite
```

## Getting the original compiled scripts

The games original scripts are not in this repository.
You must instead extract `world/*.dante` out of `COMMON.POD` yourself, then point the toolkit at the directory:

```bash
export DANTE_CORPUS=/path/to/extracted/world/
```

This is only needed for reference, or to run the fidelity tests which reference original scripts. 

## Compiling Source Files 

A `.dn` file compiles straight to a module the game will load:

```bash
dante compile mymod.dn -o world/mymod.dante
```

If you're patching an existing module rather than shipping a new one, compile to assembly
and skip the generated initialiser since the module you're patching already has one:

```bash
dante compile mypatch.dn --no-init -S -o mypatch.s
```

`.dn` is a C-like language, and its type spellings are from the VM's own prototype strings. This means a function's source signature is also its export text. 

## Decompiling to Source or ASM
To extract the original compiled scripts to `.dn` source code:
```bash
dante decompile "$DANTE_CORPUS" -o scripts/        
```

You can also target specific functions:
```bash
dante decompile world/hotel1a.dante --func spawnFlierKitchSmall
```

To instead disassemble an annotated asm listing of one function:
```bash
dante disasm world/hotel1a.dante --func spawnFlierKitchSmall
```

To then disassemble the same module, into a `global.s` file:
```bash
dante disasm world/global.dante --asm -o global.s
```

## Patching a shipped module
`dante link` splices your code into a shipped module and rewrites an existing export to
call it. The module is re-laid-out around the insertion:

```bash
dante link world/cemetery3.dante --add mypatch.s \
     --hook "void setupLevel()=void immortal_hello()" -o out/cemetery3.dante
```

## Running it without the game
The toolkit ships with a basic Dante VM, to headlessly test code.

For example, to call a single function and see what it returns:
```bash
dante vm call world/global.dante "int absInt(int)" -- -5
```

Or to start a level the way the engine does and tick the scheduler, tracing every native call as it happens:
```bash
dante vm run world/cemetery2.dante --lib world/global.dante --world dante.vm.world \
     --entry "void setupLevel()" --entry "void main()" --ticks 100 --trace
```

Every engine call is answered by a "world", which is a dict of prototype to Python callable.
Included in `dante.vm.world` is one which can run the game's scripts. 

To write your own, start from the list of what a module actually calls:
```bash
dante vm natives world/cemetery2.dante
```

## Checking your work

```bash
dante verify   world/mymod.dante                # structural verification
dante compare  shipped.dante rebuilt.dante      # two builds, export by export
dante classid  CCharacter CFlyerSmall           # DYNCAST class ids
```

The two round-trip commands rebuild every module in a directory and byte-compare against the original:

```bash
dante roundtrip     "$DANTE_CORPUS"             # parse -> emit
dante roundtrip-asm "$DANTE_CORPUS"             # parse -> .s -> assemble
```

## From Python

Everything the command line does is importable.

```python
import dante

d = dante.load("world/global.dante")            # a parsed module
m = dante.decompile_file("world/global.dante")  # readable .dn source
print(m.source())
```

Compiling returns a module you can write out yourself:

```python
built = dante.compile_source("mine.dn", libs=["world/global.dante"])
open("mine.dante", "wb").write(built.emit().encode("latin-1"))
```

And the VM runs it in-process:

```python
from dante.vm import VM
from dante.vm.world import make_world

vm = VM([d], world=make_world())
vm.init_modules()
vm.call("int absInt(int)", -5)                  # -> 5
```

## Toolkit Verification

`pytest` runs 226 tests, 217 of those require the games original scripts.

```bash
DANTE_CORPUS=/path/to/world pytest      # needed for 217/226 tests
pytest                                  # corpus tests skip, the rest still run
pytest --corpus=/path/to/world          # note the `=` (see tests/conftest.py)
```

| check | result |
|---|---|
| `.dante` → parse → emit | **21/21 byte-identical** |
| `.dante` → `.s` → assemble | **21/21 byte-identical** |
| structural verification | 21/21 clean |
| compiler reproduces shipped functions | **8/8 word-identical**, and all 21 module initialisers |
| decompile → recompile, whole corpus | **93.1% word-identical** (4780/5132), 99.6% recompile, 20 stubbed bodies |
| whole-module rebuild | 5153 exports, 4801 word-identical, **0 exports lost**, no call-graph change outside the ledger |
| link relocation | **136/136 jumps preserved**, no export lost |
| headless VM | every shipped module's `init_modules()` + `setupLevel()` runs without a fault |

The 6.9% that doesn't come back word-identical is mostly temporary-slot numbering: 283 of
the 332 differing functions have the same opcodes in the same order, just different slot
numbers. `tests/test_decompile_roundtrip.py` prints the full breakdown, and holds each
rate to a floor so a regression fails the run.

## Symbol data

Two databases ship in `src/dante/data/`, both metadata rather than game content.

`dante_api.json` is the script API registered by the game engine at startup. It contains every registered class, its method table, and its properties. 
`dante_symbols.json` is what the compiler resolves against: 505 native prototypes 74 struct-layout assumptions, and 85 enum constants 

Either can be replaced without touching code:

```bash
DANTE_API_JSON=/path/to/dante_api.json   # another build's API table
DANTE_SYMBOLS=extra.json:more.json       # extra symbols, loaded after the packaged ones
```

## Layout

```
src/dante/
    module.py       the container: parse, disassemble, assemble, verify, compare, link
    compiler.py     .dn source -> compiled module (or assembly)
    decompiler.py   compiled module -> readable .dn source
    symbols.py      where the symbol databases come from
    cli.py          the `dante` command
    vm/machine.py   the headless interpreter
    vm/world.py     GameWorld: a fake engine to run the shipped scripts against
    data/           dante_api.json, dante_symbols.json
tests/              pytest suite; tests/data holds the .dn sources it compiles
docs/               the formats, the machine, the language, the recipes
```

