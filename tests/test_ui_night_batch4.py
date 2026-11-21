"""ui_night._problems: blank or missing reasons never crash the night page."""
from __future__ import annotations

from auditor import ui_night


def _m(status, summary="", reason="", id_="r-m-001"):
    return {"id": id_, "status": status, "summary": summary, "escalate_reason": reason}


def test_whitespace_only_reason_falls_back_instead_of_crashing():
    out = ui_night._problems([_m("FAILED", reason="\n  \n", summary="  tool crashed\nmore")], None)
    assert out[0]["text"] == "failed: tool crashed"
    out = ui_night._problems([_m("ESCALATED", reason=" ", summary="\n")], None)
    assert out[0]["text"] == "escalated: no reason recorded"


def test_missing_or_none_summary_does_not_crash():
    missions = [{"id": "a", "status": "RUNNING", "summary": None, "escalate_reason": None},
                {"id": "b", "status": "DONE"},
                _m("PARTIAL", summary="\n  repo not found on disk\n", id_="c")]
    out = ui_night._problems(missions, None)
    assert out == [{"kind": "mission", "text": "incomplete: repo not found on disk", "missions": ["c"]}]
