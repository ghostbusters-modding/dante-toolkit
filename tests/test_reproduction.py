"""Hand-written `.dn` for eight shipped functions must compile to the same words."""
import os

import pytest

import dante

# source file -> (shipped module, [exported prototype])
CASES = [
    ("global_lib.dn", "global", [
        "int absInt(int)",
        "Vector getVector(float,float,float)",
        "bool isActorGroupDead(@CActorGroup)",
        "float round(float)",
        "void wait(float)",
        "void enableActorGroup(@CActorGroup,bool)",
        "void debugPrintGroup(@CActorGroup)",
    ]),
    ("hotel1a_spawn.dn", "hotel1a", [
        "void spawnFlierKitchSmall(@CSpawn)",
    ]),
]

PARAMS = [(src, mod, proto) for src, mod, protos in CASES for proto in protos]


def diff_lines(shipped, built, proto):
    """The instructions where two builds of one export disagree."""
    a = dante.func_body(shipped, proto)
    b = dante.func_body(built, proto)
    if a is None:
        return ["%s: not exported by the shipped module" % proto]
    if b is None:
        return ["%s: not produced by the compiler" % proto]
    na = [dante.normalize(shipped, i) for i in a]
    nb = [dante.normalize(built, i) for i in b]
    if na == nb:
        return []
    out = []
    for i in range(max(len(na), len(nb))):
        x = na[i] if i < len(na) else None
        y = nb[i] if i < len(nb) else None
        if x != y:
            out.append("  #%d shipped: %s" % (i, shipped.fmt_instr(a[i]) if i < len(a) else "-"))
            out.append("  #%d built  : %s" % (i, built.fmt_instr(b[i]) if i < len(b) else "-"))
    return out[:20]


@pytest.fixture(scope="module")
def built(corpus, data_dir):
    """Each source compiled once, against the shipped global.dante."""
    lib = os.path.join(corpus, "global.dante")
    return {src: dante.compile_source(os.path.join(data_dir, src), libs=[lib])
            for src, _, _ in CASES}


@pytest.mark.parametrize("src,stem,proto", PARAMS,
                         ids=[p.split("(")[0].split()[-1] for _, _, p in PARAMS])
def test_word_identical(corpus, built, report, src, stem, proto):
    shipped = dante.load(os.path.join(corpus, stem + ".dante"))
    diffs = diff_lines(shipped, built[src], proto)
    report("reproduction  %-46s %s" % (proto, "IDENTICAL" if not diffs else "DIFFERS"))
    assert not diffs, "\n".join(diffs)
