"""Round-trip every shipped export through `.dn` and compare the words back."""
import re
import tempfile
import collections

import pytest

import dante
from dante import compiler as DC
from conftest import MODULES, same_words

# Floors, not targets -- measured at 93.1% identical, 99.6% recompile, 20 stubs.
IDENTICAL_FLOOR = 93.0
RECOMPILE_FLOOR = 99.5
MAX_STUBS = 20

_LINE = re.compile(r"line (\d+):")


def build(m, tmpdir, max_rounds=400):
    """Compile the decompiled module, stubbing what the compiler rejects and
    dropping what it rejects even as a stub."""
    stub = {r.proto for r in m.results if not r.ok or r.goto_unsupported}
    drop = set()
    path = "%s/mod.dn" % tmpdir
    for _ in range(max_rounds):
        src = m.source(compilable=True, stub=stub, drop=drop)
        with open(path, "w", newline="\n") as fh:
            fh.write(src)
        try:
            return DC.compile_source(path, module=m.stem), stub | drop
        except Exception as ex:
            hit = _LINE.search(str(ex))
            victim = None
            if hit:
                line = int(hit.group(1))
                victim = next((proto for proto, (a, b) in m.line_of.items()
                               if a <= line <= b and proto not in stub), None)
            if victim is None:
                victim = next((proto for proto in m.line_of
                               if proto not in stub or proto not in drop), None)
            if victim is None:
                return None, stub | drop
            (drop if victim in stub else stub).add(victim)
    return None, stub | drop


def why_different(shipped, built, proto):
    """Classify one export's first differing instruction."""
    a = dante.func_body(shipped, proto)
    b = dante.func_body(built, proto)
    na = [dante.normalize(shipped, i) for i in a]
    nb = [dante.normalize(built, i) for i in b]
    for i in range(max(len(na), len(nb))):
        x = na[i] if i < len(na) else None
        y = nb[i] if i < len(nb) else None
        if x == y:
            continue
        sx = shipped.fmt_instr(a[i]) if i < len(a) else ""
        if x and x[0] == "LEA" and "*{D:" in sx and (not y or y[0] != "LEA"):
            return "no address-of operator (`LEA global` as a value)"
        if x and y and x[0] == "MOV" and y[0] in ("SUBF", "SUBI") and "#-" in sx:
            return "no negative literals (`-1` compiles to SUBI/SUBF)"
        if x and y and x[0] in ("FTOI", "ITOF", "ITOS", "FTOS", "BTOS", "VTOS") and y[0] == "MOV":
            return "conversions are not dest-forwarded by the compiler"
        if x and y and x[0] == y[0] == "ENTER":
            continue                     # the frame size follows from the rest
        if x and y and x[0] == y[0]:
            return "same opcodes, different slot numbers (temp allocation)"
        return "other: %s -> %s" % (x[0] if x else "-", y[0] if y else "-")
    return "?"


@pytest.fixture(scope="module")
def recompiled(decompiled):
    """stem -> one module's round-trip result, computed once."""
    cache = {}

    def get(stem):
        if stem in cache:
            return cache[stem]
        m, shipped = decompiled(stem)
        with tempfile.TemporaryDirectory() as td:
            built, stubbed = build(m, td)
        row = dict(stem=stem, total=len(m.results), ident=0, rec=0, stub=len(stubbed),
                   compiled=built is not None, init="-", causes=collections.Counter())
        for r in m.results:
            if not r.ok or r.proto in stubbed or built is None:
                continue
            if same_words(shipped, built, r.proto):
                row["ident"] += 1
            else:
                row["rec"] += 1
                row["causes"][why_different(shipped, built, r.proto)] += 1
        row["stub"] = row["total"] - row["ident"] - row["rec"]
        if m.init_result is not None and built is not None:
            row["init"] = ("identical" if same_words(shipped, built, "void __%s_init()" % m.stem)
                           else "differs")
        cache[stem] = row
        return row

    return get


@pytest.mark.parametrize("stem", MODULES)
def test_module_recompiles(stem, corpus_modules, recompiled):
    if stem not in corpus_modules:
        pytest.skip("%s not in the corpus" % stem)
    row = recompiled(stem)
    assert row["compiled"], "%s: the decompiled module does not compile" % stem
    assert row["init"] == "identical", "%s: __%s_init() changed" % (stem, stem)


def test_corpus_round_trip_rates(corpus_modules, recompiled, report):
    rows = [recompiled(s) for s in corpus_modules]
    total = sum(r["total"] for r in rows)
    ident = sum(r["ident"] for r in rows)
    rec = sum(r["rec"] for r in rows)
    stub = sum(r["stub"] for r in rows)

    report("%-16s %5s %5s %5s %5s   %6s  %s"
           % ("module", "fns", "same", "recmp", "stub", "ident%", "__init"))
    for r in rows:
        report("%-16s %5d %5d %5d %5d   %5.1f%%  %s"
               % (r["stem"], r["total"], r["ident"], r["rec"], r["stub"],
                  100.0 * r["ident"] / max(1, r["total"]), r["init"]))
    report("%-16s %5d %5d %5d %5d   %5.1f%%"
           % ("TOTAL", total, ident, rec, stub, 100.0 * ident / max(1, total)))
    report("identical %.1f%%   recompiles %.1f%%   stubbed %.1f%%"
           % (100.0 * ident / total, 100.0 * (ident + rec) / total, 100.0 * stub / total))
    causes = collections.Counter()
    for r in rows:
        causes.update(r["causes"])
    report("why the %d recompiled-but-different functions differ:" % rec)
    for cause, n in causes.most_common():
        report("  %5d  %s" % (n, cause))

    assert 100.0 * ident / total >= IDENTICAL_FLOOR
    assert 100.0 * (ident + rec) / total >= RECOMPILE_FLOOR
    assert stub <= MAX_STUBS
