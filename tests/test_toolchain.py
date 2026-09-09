"""The whole toolchain over a module we compile ourselves -- no game files needed."""
import pytest

import dante
from dante import symbols
from dante.vm import VM, World

SOURCE = """\
int twice(int n)
{
    return n * 2;
}

int sumTo(int n)
{
    int total = 0;
    for (int i = 1; i <= n; i++) {
        total += i;
    }
    return total;
}

String greet(String who)
{
    return "hello " + who;
}
"""


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    """SOURCE compiled, written out, and parsed back."""
    d = tmp_path_factory.mktemp("tiny")
    src = d / "tiny.dn"
    src.write_text(SOURCE, newline="\n")
    built = dante.compile_source(str(src))
    out = d / "tiny.dante"
    out.write_text(built.emit(), newline="")
    return str(src), str(out), built


def test_compiled_module_verifies(tiny):
    _, path, _ = tiny
    assert dante.verify_one(dante.load(path), quiet=True) == []


def test_reemit_is_byte_identical(tiny):
    _, path, built = tiny
    assert dante.load(path).emit() == built.emit()


def test_assembly_roundtrip_is_byte_identical(tiny):
    _, path, built = tiny
    d = dante.load(path)
    assert dante.parse_asm(dante.to_asm(d), d.name).emit() == built.emit()


def test_decompiles_and_recompiles_to_the_same_words(tiny, tmp_path):
    _, path, built = tiny
    m = dante.decompile_file(path)
    assert all(r.ok for r in m.results), [r.error for r in m.results if not r.ok]
    src = tmp_path / "again.dn"
    src.write_text(m.source(compilable=True), newline="\n")
    again = dante.compile_source(str(src), module="tiny")
    for proto in ("int twice(int)", "String greet(String)"):
        assert [dante.normalize(built, i) for i in dante.func_body(built, proto)] \
            == [dante.normalize(again, i) for i in dante.func_body(again, proto)]


@pytest.mark.parametrize("proto,args,want", [
    ("int twice(int)", (21,), 42),
    ("int sumTo(int)", (10,), 55),
    ("String greet(String)", ("world",), "hello world"),
])
def test_runs_in_the_vm(tiny, proto, args, want):
    _, path, _ = tiny
    vm = VM([dante.load(path)], world=World())
    vm.init_modules()
    assert vm.call(proto, *args) == want


# ------------------------------------------------------------------- symbols
def test_class_ids_match_the_engine():
    assert dante.classid("CCharacter") == 0x75C2BB57
    assert dante.class_table()[dante.classid("CCharacter")] == "CCharacter"


def test_symbol_databases_are_packaged():
    kinds = dict((k, p) for k, p in symbols.default_symbol_sources())
    assert "api" in kinds and "json" in kinds
    assert symbols.api_table()["classes"]
