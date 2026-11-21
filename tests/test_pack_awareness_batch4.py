"""An orphaned start marker must never let a later splice delete user text (e36fd0dfc0, ac86d8c708)."""
from __future__ import annotations

import pytest

from auditor import pack_awareness as pa

CASES = [
    (pa._splice_section, pa.MARKER_START, pa.MARKER_END),
    (pa._splice_pr, pa.PR_MARKER_START, pa.PR_MARKER_END),
]


def _section(start: str, end: str, body: str) -> str:
    return f"{start}\n{body}\n{end}\n"


@pytest.mark.parametrize("splice,start,end", CASES)
def test_orphaned_start_marker_never_eats_user_text(splice, start, end) -> None:
    doc = f"intro\n{start}\nuser kept writing here\nmore user text\n"  # end marker was deleted
    once = splice(doc, _section(start, end, "v1"))
    twice = splice(once, _section(start, end, "v2"))
    for text in (once, twice):
        assert "user kept writing here" in text
        assert "more user text" in text
        assert text.count(end) == 1
    assert "v2" in twice and "v1" not in twice


@pytest.mark.parametrize("splice,start,end", CASES)
def test_stale_start_above_a_block_does_not_widen_the_replacement(splice, start, end) -> None:
    # What an earlier run left behind: an orphan start, user text, then a managed block
    doc = f"intro\n{start}\nuser text\n\n{_section(start, end, 'old')}"
    out = splice(doc, _section(start, end, "new"))
    assert "user text" in out
    assert "new" in out and "old" not in out


@pytest.mark.parametrize("splice,start,end", CASES)
def test_a_well_formed_block_is_replaced_in_place(splice, start, end) -> None:
    doc = f"intro\n\n{_section(start, end, 'old')}\noutro\n"
    out = splice(doc, _section(start, end, "new"))
    assert out.startswith("intro") and "outro" in out
    assert "new" in out and "old" not in out
    assert out.count(start) == 1
