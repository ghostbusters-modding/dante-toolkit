"""Rebuild each shipped module from its own decompilation: lose no export, and
change no call graph outside KNOWN_CALL_DIFFS."""
import tempfile

import pytest

import dante
from dante import compiler as DC
from dante import decompiler as DD
from conftest import MODULES

MAX_STUBS = 20

# Accepted call-graph drift, "order" = the decompiler rotated a loop, "callee" = the
# compiler's overload fallback picked a sibling class.  See docs/dante_lang.md.
KNOWN_CALL_DIFFS = {
    "firehouse": {
        "void VigoLine()": "order",
    },
    "hotel1a": {
        "@CActor findClosestActorInGroup(Vector,@CActorGroup)": "order",
        "void throwStuffAtPlayer_12thFloor()": "callee",
        "void physmo_ballrm_chandSmall_preGetHurt(@CActor,@SDamageInfo)": "callee",
    },
    "timessquare1": {
        "void setupCeilingFans()": "callee",
        "@CFloater _3mSpawnhobo(int)": "callee",
    },
    "timessquare2": {
        "void trig_breakLobbyVent_onActorEnter(@CTrigger,@CActor)": "order",
        "void trig_breakOfficeVent_onActorEnter(@CTrigger,@CActor)": "order",
        "void trig_breakHallwayVent_onActorEnter(@CTrigger,@CActor)": "order",
    },
    "library1b": {
        "void dishevelCoal()": "callee",
        "@CActor findClosestActorInGroup(Vector,@CActorGroup)": "order",
    },
    "library2": {
        "@CActor findClosestActorInGroup(Vector,@CActorGroup)": "order",
        "void formAzetlor()": "callee",
    },
    "museum2": {
        "@CActor findClosestActorInGroup(Vector,@CActorGroup)": "order",
    },
    "museum3": {
        "@CActor findClosestActorInGroup(Vector,@CActorGroup)": "order",
    },
    "13th_floor_boss": {
        "void swFight()": "order",
    },
    "lost_island": {
        "void Setup_Ray()": "order",
        "void EndofMineCar()": "callee",
    },
    "cemetery2": {
        "void checkpoint_Underground()": "callee",
        "void setupPhysmos()": "callee",
        "void GateWeightComplete()": "callee",
        "void launchCoffins2()": "order",
        "void endStoneAngelGateThread()": "callee",
    },
    "abyss": {
        "void ArchitectFight()": "order",
    },
}


@pytest.fixture(scope="module")
def rebuilt(decompiled):
    """stem -> what `--compilable` source rebuilds into, computed once."""
    cache = {}

    def get(stem):
        if stem in cache:
            return cache[stem]
        m, shipped = decompiled(stem)
        src, stub = DD.compilable_source(m)
        built, error = None, None
        with tempfile.TemporaryDirectory() as td:
            path = "%s/%s.dn" % (td, m.stem)
            with open(path, "w", newline="\n") as fh:
                fh.write(src)
            try:
                built = DC.compile_source(path, module=m.stem)
            except Exception as ex:
                error = str(ex)
        row = dict(stem=stem, exports=len(m.funcs), stub=sorted(stub), error=error,
                   goto=sorted(r.proto for r in m.results if r.goto_fallback),
                   unsupported=sorted(r.proto for r in m.results if r.goto_unsupported),
                   same=[], missing=[], changed=[], known=[])
        if built is not None:
            same, _diff, missing = dante.compare_modules(shipped, built)
            changed = [p for p in dante.compare_calls(shipped, built) if p not in stub]
            known = KNOWN_CALL_DIFFS.get(stem, {})
            row.update(same=same, missing=missing,
                       changed=[p for p in changed if p not in known],
                       known=[p for p in changed if p in known])
        cache[stem] = row
        return row

    return get


def _row(stem, corpus_modules, rebuilt):
    if stem not in corpus_modules:
        pytest.skip("%s not in the corpus" % stem)
    return rebuilt(stem)


@pytest.mark.parametrize("stem", MODULES)
def test_module_rebuilds(stem, corpus_modules, rebuilt, report):
    row = _row(stem, corpus_modules, rebuilt)
    assert row["error"] is None, "%s did not compile: %s" % (stem, row["error"])
    report("%-16s %5d exports %4d goto %5d same %3d stub %3d known-call-diff"
           % (stem, row["exports"], len(row["goto"]), len(row["same"]),
              len(row["stub"]), len(row["known"])))


@pytest.mark.parametrize("stem", MODULES)
def test_no_export_goes_missing(stem, corpus_modules, rebuilt):
    row = _row(stem, corpus_modules, rebuilt)
    assert row["missing"] == []


@pytest.mark.parametrize("stem", MODULES)
def test_no_unexpected_call_graph_change(stem, corpus_modules, rebuilt):
    row = _row(stem, corpus_modules, rebuilt)
    assert row["changed"] == [], (
        "%s: call targets changed outside KNOWN_CALL_DIFFS" % stem)


@pytest.mark.parametrize("stem", MODULES)
def test_exception_scopes_fold_back(stem, corpus_modules, rebuilt):
    row = _row(stem, corpus_modules, rebuilt)
    assert row["unsupported"] == [], (
        "%s: exception scope did not fold back into try/catch" % stem)


def test_stub_ceiling(corpus_modules, rebuilt, report):
    rows = [rebuilt(s) for s in corpus_modules]
    stubs = [(r["stem"], p) for r in rows for p in r["stub"]]
    report("whole module: %d exports, %d rebuilt word-identical, %d goto bodies, %d stubbed"
           % (sum(r["exports"] for r in rows), sum(len(r["same"]) for r in rows),
              sum(len(r["goto"]) for r in rows), len(stubs)))
    for stem, proto in stubs:
        report("  stubbed  %-16s %s" % (stem, proto))
    assert len(stubs) <= MAX_STUBS
