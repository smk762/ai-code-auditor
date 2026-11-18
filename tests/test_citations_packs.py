"""Regression tests for wiki/citations.py audit packs (range bounds, fences, prefixes, confinement)."""
from __future__ import annotations

from pathlib import Path

import pytest

from wiki import citations

FIVE = "def one():\n    return 1\n\n\ndef two():\n"  # 5 lines


@pytest.fixture
def code(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(FIVE)
    (tmp_path / "secret.py").write_text("x = 1\n")
    return root


def test_citation_one_line_past_the_end_is_out_of_range(code: Path) -> None:
    rep = citations.check("Past the end [app.py:L6]. Spans past [app.py:L5-L6].", code)
    assert [f["reason"] for f in rep.failed] == ["range", "range"]
    assert citations.check("Last line [app.py:L5].", code).failed == []


def test_blank_lines_inside_code_fences_are_kept(code: Path) -> None:
    md = "# T\n\n```python\ndef a():\n    pass\n\n\ndef b():\n    pass\n```\n\nText.\n"
    out, _ = citations.drop_failed_claims(md, code)
    assert "    pass\n\n\ndef b():" in out


def test_blank_runs_left_by_dropped_claims_still_collapse(code: Path) -> None:
    md = "# T\n\n- gone [nope.py:L1]\n\nNext.\n"
    out, _ = citations.drop_failed_claims(md, code)
    assert out == "# T\n\nNext.\n"


def test_dropping_first_sentence_keeps_blockquote_and_indent(code: Path) -> None:
    md = ("> Bad `ghost` here [app.py:L1-L2]. Good `one` here [app.py:L1-L2].\n\n"
          "- item\n\n    Bad `ghost` again [app.py:L1-L2]. Still `one` [app.py:L1]. And `two` [app.py:L5].\n")
    out, dropped = citations.drop_failed_claims(md, code)
    assert "> Good `one` here [app.py:L1-L2]." in out.splitlines()
    assert "    Still `one` [app.py:L1]. And `two` [app.py:L5]." in out.splitlines()
    assert len(dropped) == 2


@pytest.mark.parametrize("absolute", [False, True])
def test_cited_paths_outside_the_root_are_never_read(code: Path, absolute: bool) -> None:
    path = str(code.parent / "secret.py") if absolute else "../secret.py"
    md = f"x [{path}:L1]"
    assert citations.cited_files(md, code) == {}
    assert citations.cited_ranges(md, code) == {}


def test_cited_code_flags_out_of_range_and_reversed_citations(code: Path) -> None:
    shown = citations.cited_code("a [app.py:L50-L60] b [app.py:L4-L2] c [app.py:L1]", code, pad=0)
    assert "app.py L50-L60 (LINES DO NOT EXIST; file has 5 lines)" in shown
    assert "app.py L4-L2 (LINES DO NOT EXIST; file has 5 lines)" in shown
    assert "===== app.py L1-L1 =====\n    1| def one():" in shown
    assert "L47" not in shown and "L4-L1" not in shown
