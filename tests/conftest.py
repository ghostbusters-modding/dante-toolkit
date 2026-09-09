"""Shared fixtures.  Anything touching the shipped scripts needs $DANTE_CORPUS."""
import os

import pytest

import dante
from dante import decompiler as DD

# The shipped modules, in the order the reports table them.
MODULES = ["global", "firehouse", "hotel1a", "hotel1b", "timessquare1", "timessquare1b",
           "timessquare2", "boss_sp_side", "library1a", "library1b", "library2",
           "museum1", "museum2", "museum3", "hotel2", "13th_floor_boss",
           "lost_island", "lost_island2", "cemetery1", "cemetery2", "abyss"]

_REPORT = []


def same_words(shipped, built, proto):
    """Same instruction words and fixups, ignoring string-pool offsets?"""
    a = dante.func_body(shipped, proto)
    b = dante.func_body(built, proto)
    if a is None or b is None:
        return False
    return ([dante.normalize(shipped, i) for i in a]
            == [dante.normalize(built, i) for i in b])


def pytest_addoption(parser):
    parser.addoption("--corpus", action="store", default=None,
                     help="directory holding the shipped world/*.dante modules; pass it "
                          "as --corpus=DIR (default: $DANTE_CORPUS)")


def corpus_dir(config):
    """The corpus directory, or None if unset or missing."""
    path = config.getoption("--corpus") or os.environ.get("DANTE_CORPUS")
    return path if path and os.path.isdir(path) else None


def pytest_generate_tests(metafunc):
    """Give `corpus_module` one test per shipped .dante file."""
    if "corpus_module" not in metafunc.fixturenames:
        return
    path = corpus_dir(metafunc.config)
    files = dante.iter_files(path) if path else []
    if not files:
        metafunc.parametrize("corpus_module", [pytest.param(
            None, marks=pytest.mark.skip(reason="no corpus (--corpus / $DANTE_CORPUS)"))])
        return
    metafunc.parametrize("corpus_module", files,
                         ids=[os.path.basename(f)[:-len(".dante")] for f in files])


@pytest.fixture(scope="session")
def corpus(request):
    path = corpus_dir(request.config)
    if path is None:
        pytest.skip("no corpus: pass --corpus <dir of shipped .dante> or set $DANTE_CORPUS")
    return path


@pytest.fixture(scope="session")
def corpus_modules(corpus):
    """The stems of MODULES actually present in the corpus."""
    return [s for s in MODULES if os.path.exists(os.path.join(corpus, s + ".dante"))]


@pytest.fixture(scope="session")
def data_dir():
    """tests/data, holding the `.dn` sources these tests compile."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


@pytest.fixture(scope="session")
def decompiled(corpus):
    """stem -> (decompiled Module, shipped Dante).  Cached: decompiling is the slow part."""
    cache = {}

    def get(stem):
        if stem not in cache:
            path = os.path.join(corpus, stem + ".dante")
            cache[stem] = (DD.decompile_file(path), dante.load(path))
        return cache[stem]

    return get


@pytest.fixture(scope="session")
def report():
    """Append a line to the report printed after the run."""
    return _REPORT.append


def pytest_terminal_summary(terminalreporter):
    if _REPORT:
        terminalreporter.write_sep("=", "dante toolchain report")
        for line in _REPORT:
            terminalreporter.write_line(line)
