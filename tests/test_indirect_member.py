"""A class member read through a parameter or local; only the corpus-proven set is safe."""
# Copyright (C) 2026 Colin Sullivan and contributors
# SPDX-License-Identifier: GPL-2.0-only
import pytest

import dante
from dante.compiler import CompileError
from dante.module import indirect_member_warnings

STRUCT = "struct CDialogDatabaseEntry { String tag; String text; }\n\n"

PARAM_TEXT = STRUCT + """\
void objective(@CDialogDatabaseEntry e)
{
    setCurrentObjective(e.text);
}
"""

PARAM_TAG = STRUCT + """\
void objective(@CDialogDatabaseEntry e)
{
    setCurrentObjective(e.tag);
}
"""

GLOBAL_TEXT = STRUCT + """\
extern CDialogDatabaseEntry objLong;

void objective()
{
    setCurrentObjective(objLong.text);
}
"""


def _compile(tmp_path, name, source, **kw):
    src = tmp_path / (name + ".dn")
    src.write_text(source, newline="\n")
    return dante.compile_source(str(src), module=name, **kw)


def test_param_member_read_faults_to_compile_error(tmp_path):
    with pytest.raises(CompileError) as ex:
        _compile(tmp_path, "probe1", PARAM_TEXT)
    msg = str(ex.value)
    assert "CDialogDatabaseEntry::text" in msg
    assert "extern" in msg


def test_tag_is_exempt(tmp_path):
    d = _compile(tmp_path, "probe2", PARAM_TAG)
    assert indirect_member_warnings(d) == []


def test_global_form_compiles(tmp_path):
    d = _compile(tmp_path, "probe3", GLOBAL_TEXT)
    assert indirect_member_warnings(d) == []


def test_allow_indirect_members_flag_downgrades_to_warning(tmp_path, capsys):
    d = _compile(tmp_path, "probe4", PARAM_TEXT, allow_indirect_members=True)
    warns = indirect_member_warnings(d)
    assert len(warns) == 1
    assert "CDialogDatabaseEntry::text" in warns[0]
    assert "CDialogDatabaseEntry::text" in capsys.readouterr().err


def test_shipped_corpus_has_no_indirect_member_warnings(corpus_module):
    """The 21 shipped modules only ever hit dante.module.PROVEN_INDIRECT_MEMBERS."""
    d = dante.load(corpus_module)
    assert indirect_member_warnings(d) == []
