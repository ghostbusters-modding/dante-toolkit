"""Every shipped module survives both round-trips byte-for-byte, and verifies."""
import dante


def test_reemit_is_byte_identical(corpus_module):
    d = dante.load(corpus_module)
    d.sort_sections()
    with open(corpus_module, "rb") as fh:
        original = fh.read()
    assert d.emit().encode("latin-1") == original


def test_assembly_roundtrip_is_byte_identical(corpus_module):
    d = dante.load(corpus_module)
    rebuilt = dante.parse_asm(dante.to_asm(d), d.name)
    with open(corpus_module, "rb") as fh:
        original = fh.read()
    assert rebuilt.emit().encode("latin-1") == original


def test_verifies(corpus_module):
    errs = dante.verify_one(dante.load(corpus_module), quiet=True)
    assert errs == [], "\n".join(errs[:10])
