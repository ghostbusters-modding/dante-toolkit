"""Splicing a patch into a shipped module must not move a single jump."""
import os

import pytest

import dante
from dante.module import NAMEOP


def jump_map(d):
    """Every jump, as (instruction index -> target instruction index)."""
    idx = {ins.off: i for i, ins in enumerate(d.code)}
    return [(i, idx.get(ins.off + dante.module.s16(ins.ops[0].words[0]), -1))
            for i, ins in enumerate(d.code) if dante.module.OPS[ins.op][1] == "j"]


@pytest.fixture(scope="module")
def linked(corpus, data_dir):
    """13th_floor_boss with `void immortal_probe()` linked in and hooked."""
    path = os.path.join(corpus, "13th_floor_boss.dante")
    if not os.path.exists(path):
        pytest.skip("13th_floor_boss.dante not in the corpus")
    shipped = dante.load(path)
    target = next(txt for k, off, txt in shipped.exports
                  if k == "C" and txt.startswith("void ") and txt.endswith("()")
                  and "__" not in txt)
    patch = dante.compile_source(os.path.join(data_dir, "link_probe.dn"), emit_init=False)
    base = dante.load(path)
    dante.link(base, patch, [(target, "void immortal_probe()", 8)], verbose=False)
    return shipped, base, target


def test_linked_module_verifies(linked):
    shipped, base, target = linked
    errs = dante.verify_one(base, quiet=True)
    assert errs == [], "\n".join(errs[:10])


def test_every_jump_is_preserved(linked, report):
    shipped, base, target = linked
    hook_at = next(i for i, ins in enumerate(base.code)
                   if ins.op == NAMEOP["CALL"]
                   and ins.ops[0].fix == [("C", "void immortal_probe()")])
    # One instruction went in at hook_at, so everything from there on shifts by one.
    expected = [(i + (i >= hook_at), t + (t >= hook_at)) for i, t in jump_map(shipped)]
    actual = jump_map(base)
    kept = sum(1 for x, y in zip(expected, actual) if x == y)
    report("link          hooked %-40s %d/%d jumps preserved" % (target, kept, len(expected)))
    assert actual[:len(expected)] == expected


def test_no_export_is_lost(linked):
    shipped, base, target = linked
    after = [t for _, _, t in base.exports]
    assert [t for k, _, t in shipped.exports if k == "C" and t not in after] == []
