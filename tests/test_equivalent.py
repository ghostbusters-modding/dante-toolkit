"""equivalent(): two builds behave the same even where their stack slots differ."""
# Copyright (C) 2026 Colin Sullivan and contributors
# SPDX-License-Identifier: GPL-2.0-only
import os
import random

import dante
from dante.module import OPS, equivalent, func_body, normalize

BASE = """\
void f(float a, float b)
{
    float x = fAbs(a);
    float y = fAbs(b);
    debugPrint(toString(x - y));
}
"""
SHIFTED = BASE.replace("{\n", "{\n    int pad;\n", 1)     # every slot moves up one
SWAPPED = SHIFTED.replace("x - y", "y - x")                 # same opcodes, other operands
PROTO = "void f(float,float)"


def _compile(tmp_path, name, source):
    src = tmp_path / (name + ".dn")
    src.write_text(source, newline="\n")
    return dante.compile_source(str(src), module=name)


def _same_words(a, b):
    return [normalize(a, i) for i in func_body(a, PROTO)] == \
        [normalize(b, i) for i in func_body(b, PROTO)]


def test_moved_slots_are_equivalent(tmp_path):
    a = _compile(tmp_path, "eqa", BASE)
    b = _compile(tmp_path, "eqb", SHIFTED)
    assert not _same_words(a, b)
    assert equivalent(a, b, PROTO)


def test_swapped_operands_are_not(tmp_path):
    a = _compile(tmp_path, "eqa", BASE)
    b = _compile(tmp_path, "eqc", SWAPPED)
    assert [i.name for i in func_body(a, PROTO)] == [i.name for i in func_body(b, PROTO)]
    assert not equivalent(a, b, PROTO)


def test_every_export_matches_itself(corpus):
    d = dante.load(os.path.join(corpus, "hotel1a.dante"))
    assert [p for p in d.func_at.values() if not equivalent(d, d, p)] == []


def test_a_moved_read_is_caught(corpus):
    """Point one read at the neighbouring slot; the check must notice every time."""
    a = dante.load(os.path.join(corpus, "hotel1a.dante"))
    b = dante.load(os.path.join(corpus, "hotel1a.dante"))
    rnd = random.Random(7)
    protos = sorted(a.func_at.values())
    missed = []
    for _ in range(150):
        p = rnd.choice(protos)
        reads = [(ins, k) for ins in func_body(b, p) for k in (1, 2)
                 if ins.ops[k].mode == 3 and OPS[ins.op][k + 1] == "r"]
        if not reads:
            continue
        ins, k = rnd.choice(reads)
        old = ins.ops[k].words
        ins.ops[k].words = ((old[0] + rnd.choice((4, -4, 8))) & 0xFFFF,) + tuple(old[1:])
        if equivalent(a, b, p):
            missed.append((p, b.fmt_instr(ins)))
        ins.ops[k].words = old
    assert missed == []
