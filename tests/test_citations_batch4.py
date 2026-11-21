"""wiki/citations.py: claims that span hard-wrapped lines are dropped whole (batch-4 pack)."""
from __future__ import annotations

from pathlib import Path

import pytest

from wiki import citations

FIVE = "def one():\n    return 1\n\n\ndef two():\n"  # 5 lines


@pytest.fixture
def code(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(FIVE)
    return tmp_path


def test_a_wrapped_sentence_is_dropped_with_its_opening(code: Path) -> None:
    md = ("# T\n\nThe app defines `one` [app.py:L1-L2]. It also wires the\n"
          "`ghost` handler [app.py:L1-L2]. Then `two`\nfollows [app.py:L5].\n")
    out, dropped = citations.drop_failed_claims(md, code)
    assert "wires" not in out and "ghost" not in out
    assert "The app defines `one` [app.py:L1-L2]. Then `two` follows [app.py:L5]." in out.splitlines()
    assert [d["claim"] for d in dropped] == ["It also wires the `ghost` handler [app.py:L1-L2]."]


def test_a_list_item_with_a_failing_citation_on_its_continuation_line_goes_whole(code: Path) -> None:
    md = "# T\n\n- `one` is kept [app.py:L1].\n- an unsupported claim that wraps\n  onto the next line [nope.py:L1].\n- last\n"
    out, dropped = citations.drop_failed_claims(md, code)
    assert "unsupported" not in out and "onto the next" not in out
    assert out == "# T\n\n- `one` is kept [app.py:L1].\n- last\n"
    assert dropped == [{"claim": "- an unsupported claim that wraps onto the next line [nope.py:L1].",
                        "reason": "missing", "citation": "[nope.py:L1]"}]


def test_wrapped_text_without_failures_is_left_as_written(code: Path) -> None:
    md = "# T\n\nA paragraph about `one`\nwrapped by hand [app.py:L1].\n\n> quoted\n> text [app.py:L5].\n"
    out, dropped = citations.drop_failed_claims(md, code)
    assert out == md and dropped == []


def test_a_wrapped_blockquote_keeps_its_marker(code: Path) -> None:
    md = "> Bad `ghost`\n> here [app.py:L1-L2]. Good `one`\n> here [app.py:L1-L2].\n"
    out, _ = citations.drop_failed_claims(md, code)
    assert out == "> Good `one` here [app.py:L1-L2].\n"
