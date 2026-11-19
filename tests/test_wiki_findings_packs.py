"""Regression tests for wiki/findings.py audit packs."""
from __future__ import annotations

from wiki.findings import _esc

BS = "\\"


def _cells(row: str) -> list[str]:
    """Split a GFM table row as cmark-gfm does: a backslash escapes the next character."""
    cells, cur, i = [], "", 0
    while i < len(row):
        if row[i] == BS and i + 1 < len(row):
            cur += row[i:i + 2]
            i += 2
            continue
        if row[i] == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += row[i]
        i += 1
    return cells[1:]  # drop the empty cell before the leading pipe


def test_backslash_before_pipe_does_not_split_the_cell() -> None:
    for text in (f"foo{BS}|bar", f"foo{BS}{BS}|bar", "a|b"):
        assert len(_cells(f"| {_esc(text)} | next |")) == 2, text


def test_every_line_break_is_collapsed() -> None:
    for brk in ("\r", "\r\n", " ", "\n"):
        assert _esc(f"a{brk}b") == "a b"


def test_falsy_values_other_than_none_are_shown() -> None:
    assert (_esc(0), _esc(False), _esc(None), _esc("")) == ("0", "False", "", "")
