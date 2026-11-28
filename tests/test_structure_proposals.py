"""An agent may propose what to move; only a person approves it (auditor/structure_proposals.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import structure
from auditor import structure_packs as sp
from auditor.agent_pack import list_packs, load_pack
from auditor.audit_api import app
from auditor.structure_proposals import edges_key, proposals_path

BASE = {"app/__init__.py": "", "app/a.py": "from app.b import go\nLIMIT = 1\n",
        "app/b.py": "def go():\n    from app.a import LIMIT\n    return LIMIT\n"}
CID = sp.cycle_id("mono", ["app/a.py", "app/b.py"])
NOTE = "Move LIMIT into app/limits.py; a uses b for real"


@pytest.fixture(autouse=True)
def out(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    monkeypatch.setenv("AI_AUDIT_STRUCTURE_DECISIONS_PATH", str(tmp_path / "rules" / "s.json"))
    monkeypatch.setenv("AI_AUDIT_STRUCTURE_PROPOSALS_PATH", str(tmp_path / "rules" / "p.json"))
    o = tmp_path / "reports"
    o.mkdir()
    _report(o, BASE)
    return o


def _report(out: Path, files: dict[str, str]) -> None:
    (out / structure.REPORT_NAME).write_text(json.dumps({"at": "t", "repos": {"mono": structure.scan_sources(files)}}))


def _row(out: Path) -> dict:
    return next(r for r in sp.ui_data(out)["cycles"] if r["id"] == CID)


def _proposal(out: Path, **kw) -> dict:
    return {"edges_key": _row(out)["edges_key"], "verdict": "fix", "confidence": "high", "note": NOTE, "author": "m",
            "remove": [{"from": "app/b.py", "to": "app/a.py"}],
            "add": [{"from": "app/a.py", "to": "app/limits.py"}, {"from": "app/b.py", "to": "app/limits.py"}], **kw}


def test_a_proposal_decides_nothing(out: Path) -> None:
    sp.propose(out, CID, _proposal(out))
    sp.sync_packs(out)
    row = _row(out)
    assert row["proposal"]["offered"] and row["decision"] is None and list_packs(out, kind="module_cycle") == []
    with pytest.raises(ValueError, match="what to move"):  # never stands in for the approver's note
        sp.decide(out, CID, "approved")
    sp.decide(out, CID, "approved", note="Something else entirely, by hand")
    sp.propose(out, CID, _proposal(out, note="A later proposal than the approval"))
    sp.sync_packs(out)
    assert load_pack(out, sp.pack_id(CID))["governance"]["cycle_decision"]["note"] == "Something else entirely, by hand"


def test_only_a_fresh_confident_fix_is_offered(out: Path) -> None:
    for kw in ({"confidence": "medium"}, {"confidence": "low"}, {"verdict": "leave", "remove": [], "add": []}):
        sp.propose(out, CID, _proposal(out, **kw))
        assert _row(out)["proposal"]["offered"] is False
        assert "proposal" not in (sp.decide(out, CID, "approved", note=NOTE) and _row(out)["decision"])
        sp.withdraw(out, CID)
    sp.propose(out, CID, _proposal(out))
    moved = {**BASE, "app/b.py": "\n\n" + BASE["app/b.py"]}
    _report(out, moved)  # only line numbers changed
    assert _row(out)["proposal"]["offered"] is True
    _report(out, {**BASE, "app/a.py": BASE["app/a.py"] + "TOP = 2\n", "app/b.py": BASE["app/b.py"].replace("import LIMIT", "import LIMIT, TOP")})
    assert _row(out)["proposal"]["stale"] is True and _row(out)["proposal"]["offered"] is False


def test_a_proposal_that_leaves_a_circle_is_refused(out: Path) -> None:
    relay = [{"from": "app/b.py", "to": "app/relay.py"}, {"from": "app/relay.py", "to": "app/a.py"}]
    for bad, match in (({"remove": []}, "still there"), ({"add": relay}, "still there"),
                       ({"remove": [{"from": "x.py", "to": "y.py"}]}, "doesn't have"),
                       ({"remove": [{"to": "y.py"}]}, "list of"), ({"edges_key": "old"}, "other imports"),
                       ({"note": "short"}, "note of"), ({"note": "x" * 2001}, "note of"), ({"author": " "}, "author"),
                       ({"verdict": "ok"}, "verdict"), ({"confidence": "sure"}, "confidence")):
        with pytest.raises(ValueError, match=match):
            sp.propose(out, CID, _proposal(out, **bad))
    assert not Path(proposals_path()).exists()
    assert sp.propose(out, "cyc-nope", _proposal(out)) is None


@pytest.mark.parametrize(("note", "used"), [(NOTE, "as_written"), (NOTE + ", and keep the name", "edited")])
def test_the_decision_records_whether_the_proposal_was_edited(out: Path, note: str, used: str) -> None:
    sp.propose(out, CID, _proposal(out))
    sp.decide(out, CID, "approved", note=note)
    assert _row(out)["decision"]["proposal"] == {"used": used, "author": "m", "note": NOTE, "at": _row(out)["proposal"]["at"]}


def test_an_unreadable_proposals_file_never_blocks_a_decision(out: Path) -> None:
    p = Path(proposals_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{nope")
    assert _row(out)["proposal"] is None
    assert sp.decide(out, CID, "approved", note=NOTE)["status"] == "open"
    with pytest.raises(ValueError):  # and a new proposal never overwrites it
        sp.propose(out, CID, _proposal(out))
    assert p.read_text() == "{nope"


def test_the_endpoint_needs_auth_and_maps_errors(out: Path, monkeypatch) -> None:
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(out))
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "s3cret")
    client, auth, url = TestClient(app), {"X-API-Token": "s3cret"}, f"/audit/cycles/{CID}/proposal"
    body = _proposal(out)
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json={**body, "remove": []}, headers=auth).status_code == 422
    assert client.post("/audit/cycles/cyc-nope/proposal", json=body, headers=auth).status_code == 404
    assert client.post(url, json=body, headers=auth).json()["offered"] is True
    row = client.get("/audit/architecture.json").json()["cycles"]["cycles"][0]
    assert row["proposal"]["note"] == NOTE and row["edges_key"] == edges_key(row["edges"])


def test_the_read_endpoints_say_where_each_proposal_stands(out: Path, monkeypatch) -> None:
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(out))
    client, url = TestClient(app), f"/audit/cycles/{CID}/proposal"
    got = client.get(url).json()  # nothing proposed yet: still what an agent needs to write one
    assert got["proposal"] is None and got["proposal_state"] == "none"
    assert got["edges_key"] == edges_key(got["edges"]) and got["note_min"] == sp.NOTE_MIN
    assert client.get("/audit/cycles/cyc-nope/proposal").status_code == 404
    assert client.get("/audit/cycles.json?proposal=none").json()["cycles"][0]["id"] == CID

    sp.propose(out, CID, _proposal(out, confidence="medium"))
    got = client.get(url).json()
    assert got["proposal_state"] == "review" and got["proposal"]["note"] == NOTE and not got["proposal"]["offered"]
    listed = client.get("/audit/cycles.json").json()["cycles"]
    assert [(c["id"], c["proposal_state"], c["proposal"]["confidence"]) for c in listed] == [(CID, "review", "medium")]
    assert "edges" not in listed[0] and "note" not in listed[0]["proposal"]
    assert client.get("/audit/cycles.json?proposal=none").json()["cycles"] == []
    assert client.get("/audit/cycles.json?repo=other").json()["cycles"] == []
    assert client.get("/audit/cycles.json?proposal=maybe").status_code == 422

    sp.propose(out, CID, _proposal(out))
    assert client.get(url).json()["proposal_state"] == "offered"
    _report(out, {**BASE, "app/a.py": BASE["app/a.py"] + "TOP = 2\n",
                  "app/b.py": BASE["app/b.py"].replace("import LIMIT", "import LIMIT, TOP")})
    assert client.get(url).json()["proposal_state"] == "stale"
