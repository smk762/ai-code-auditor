"""auditor/ui_arch.py: a malformed candidates report or an unreadable decisions file is reported,
never a crash or "no data"."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor import ui_arch


@pytest.mark.parametrize("text", ["candidates: [1, 2\n", "- just\n- a list\n"], ids=["truncated", "not_a_mapping"])
def test_a_malformed_candidates_report_is_an_error_not_a_crash(tmp_path: Path, text: str) -> None:
    (tmp_path / "extraction_candidates.yaml").write_text(text)
    dec = tmp_path / "d.json"
    dec.write_text(json.dumps({"decisions": {"c1": {"status": "approved", "title": "kept"}}}))
    got = ui_arch._candidates(tmp_path, dec)
    assert got["candidates_error"] and [c["id"] for c in got["candidates"]] == ["c1"]  # decisions still shown


def test_an_unreadable_decisions_file_is_reported_with_no_candidates_file(tmp_path: Path) -> None:
    dec = tmp_path / "d.json"
    dec.write_text('{"decisions": [1]}')
    got = ui_arch._candidates(tmp_path, dec)
    assert got is not None and got["decisions_error"] and got["candidates"] == []
    dec.unlink()
    assert ui_arch._candidates(tmp_path, dec) is None
