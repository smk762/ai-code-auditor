"""A module cycle becomes a pack only when a person approves it (auditor/structure_packs.py)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import structure
from auditor import structure_packs as sp
from auditor.agent_pack import list_packs, load_pack, rebuild_index, update_pack_status, write_pack
from auditor.audit_api import app
from auditor.decisions import DecisionConflict

NOTE = "Move LIMIT into app/limits.py; a uses b for real"
BASE = {"app/__init__.py": "", "app/a.py": "from app.b import go\nLIMIT = 1\n",
        "app/b.py": "def go():\n    from app.a import LIMIT\n    return LIMIT\n", "app/c.py": "x = 1\n"}
RING = {**BASE, "app/b.py": "from app import c\n\n\ndef go():\n    return c\n", "app/c.py": "from app.a import LIMIT\n"}
FIXED = {**BASE, "app/b.py": "def go():\n    return 1\n"}
CID = sp.cycle_id("mono", ["app/a.py", "app/b.py"])
PID = sp.pack_id(CID)


@pytest.fixture(autouse=True)
def out(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    monkeypatch.setenv("AI_AUDIT_STRUCTURE_DECISIONS_PATH", str(tmp_path / "rules" / "s.json"))
    o = tmp_path / "reports"
    o.mkdir()
    _report(o, BASE)
    return o


def _report(out: Path, files: dict[str, str] | None) -> None:
    scan = structure.scan_sources(files) if files else {"error": "no dev", "complete": False}
    (out / structure.REPORT_NAME).write_text(json.dumps({"at": "t", "repos": {"mono": scan}}))


def _status(out: Path) -> str | None:
    return (load_pack(out, PID) or {}).get("status")


def _open(out: Path) -> list[str]:
    return [r["id"] for r in sp.ui_data(out)["cycles"] if not r["decision"]]


def test_a_reported_cycle_has_no_pack_until_approved(out: Path) -> None:
    sp.sync_packs(out)
    assert list_packs(out, kind="module_cycle") == [] and _open(out) == [CID]
    assert sp.decide(out, CID, "approved", note=NOTE)["status"] == "open"
    pack = load_pack(out, PID)
    assert pack["governance"]["structure"]["members"] == ["app/a.py", "app/b.py"] and NOTE in pack["narrative"]["description"]
    fixed = structure.scan_sources(FIXED)
    assert structure.judge(pack, structure.scan_sources(BASE), fixed)["gone"] is True  # what validate_patch will ask


def test_approval_needs_a_note_and_a_reported_cycle(out: Path) -> None:
    for bad in ("", "fix it", " " * 40):
        with pytest.raises(ValueError, match="what to move"):
            sp.decide(out, CID, "approved", note=bad)
    with pytest.raises(ValueError, match="status"):
        sp.decide(out, CID, "maybe", note=NOTE)
    assert sp.decide(out, "cyc-nope", "approved", note=NOTE) is None
    sp.decide(out, CID, "dismissed")
    _report(out, FIXED)
    with pytest.raises(ValueError, match="nothing to approve"):
        sp.decide(out, CID, "approved", note=NOTE)
    assert list_packs(out, kind="module_cycle") == []


@pytest.mark.parametrize("held", ["claimed", "in_progress", "unverified"])
def test_a_decision_never_moves_a_held_pack(out: Path, held: str) -> None:
    sp.decide(out, CID, "approved", note=NOTE)
    update_pack_status(out, PID, held)
    for call in (lambda: sp.decide(out, CID, "dismissed"), lambda: sp.withdraw(out, CID),
                 lambda: sp.decide(out, CID, "approved", note=NOTE + " now")):
        with pytest.raises(DecisionConflict):
            call()
    for files in (FIXED, None):
        _report(out, files)
        sp.sync_packs(out)
    assert _status(out) == held
    assert load_pack(out, PID)["governance"]["cycle_decision"]["note"] == NOTE


def test_dismissing_and_withdrawing_close_an_idle_pack(out: Path) -> None:
    sp.decide(out, CID, "approved", note=NOTE)
    sp.decide(out, CID, "dismissed", note="intended")
    assert _status(out) == "wontfix" and _open(out) == []
    assert sp.withdraw(out, CID)["pack_status"] == "obsolete" and _open(out) == [CID]
    assert sp.decide(out, CID, "approved", note=NOTE)["status"] == "open"
    assert sp.withdraw(out, "cyc-nope") is None


def test_sync_closes_unapproved_and_gone_packs_but_never_on_unknown(out: Path) -> None:
    sp.decide(out, CID, "approved", note=NOTE)
    stray = sp.build_pack("cyc-stray", {"repo": "mono", "members": ["x.py", "y.py"], "note": NOTE})
    write_pack(stray, out)
    rebuild_index(out)
    assert sp.sync_packs(out)["unapproved"] == 1
    assert load_pack(out, "pack-cyc-stray")["status"] == "obsolete" and _status(out) == "open"
    for unknown in (None, {**FIXED, "app/c.py": "def (:\n"}):  # unreadable repo, then one unreadable file
        _report(out, unknown)
        assert sp.sync_packs(out)["unknown"] == 1 and _status(out) == "open"
    (out / structure.REPORT_NAME).unlink()
    assert sp.sync_packs(out)["unknown"] == 1 and _status(out) == "open"
    _report(out, FIXED)
    assert sp.sync_packs(out)["gone"] == 1 and _status(out) == "obsolete"  # no longer reported, never "resolved"
    _report(out, BASE)
    assert sp.sync_packs(out)["reopened"] == 1 and _status(out) == "open"


def test_sync_leaves_a_resolved_pack_whose_fix_is_not_merged_yet(out: Path) -> None:
    sp.decide(out, CID, "approved", note=NOTE)
    update_pack_status(out, PID, "resolved")
    sp.sync_packs(out)
    assert _status(out) == "resolved"


def test_a_changed_cycle_follows_its_decision_by_members(out: Path) -> None:
    sp.decide(out, CID, "dismissed")
    _report(out, RING)  # it gained app/c.py: a new proposal
    assert _open(out) == [sp.cycle_id("mono", ["app/a.py", "app/b.py", "app/c.py"])]
    sp.withdraw(out, CID)
    ring = _open(out)[0]
    sp.decide(out, ring, "dismissed")
    _report(out, BASE)  # it shrank: still dismissed
    assert _open(out) == []
    sp.withdraw(out, ring)
    sp.decide(out, CID, "approved", note=NOTE)
    _report(out, RING)  # an approved cycle that grew is the same work, with the members it was approved on
    assert _open(out) == [] and sp.sync_packs(out)["gone"] == 0 and _status(out) == "open"
    assert sp.decide(out, CID, "approved", note=NOTE + " still")["status"] == "open"
    assert load_pack(out, PID)["governance"]["structure"]["members"] == ["app/a.py", "app/b.py"]


def test_the_same_paths_in_another_repo_are_another_cycle(out: Path) -> None:
    sp.decide(out, CID, "dismissed")
    scan = structure.scan_sources(BASE)
    (out / structure.REPORT_NAME).write_text(json.dumps({"repos": {"mono": scan, "other": scan}}))
    assert _open(out) == [sp.cycle_id("other", ["app/a.py", "app/b.py"])]


def test_nightly_sync_needs_a_scan_that_finished() -> None:
    src = (Path(__file__).resolve().parents[1] / "pipelines/nightly_repo_audit.py").read_text()
    guards = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.If)
              and any("sync_cycle_packs(" in ast.unparse(b) for b in n.body)]
    assert guards and ast.unparse(guards[-1].test) == "scanned and (not scanned.get('error'))"


def test_endpoints_need_auth_and_map_errors(out: Path, monkeypatch) -> None:
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(out))
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "s3cret")
    client, auth = TestClient(app), {"X-API-Token": "s3cret"}
    url = f"/audit/cycles/{CID}/decision"
    body = {"status": "approved", "note": NOTE}
    assert client.post(url, json=body).status_code == 401 and client.delete(url).status_code == 401
    assert client.post(url, json={"status": "approved"}, headers=auth).status_code == 422
    assert client.post("/audit/cycles/cyc-nope/decision", json=body, headers=auth).status_code == 404
    assert client.post(url, json=body, headers=auth).json()["pack_id"] == PID
    assert client.get("/audit/architecture.json").json()["cycles"]["cycles"][0]["pack"] == {"id": PID, "status": "open"}
    update_pack_status(out, PID, "claimed")
    assert client.delete(url, headers=auth).status_code == 409
    update_pack_status(out, PID, "open")
    assert client.delete(url, headers=auth).json()["pack_status"] == "obsolete"
    monkeypatch.setenv("AI_AUDIT_STRUCTURE_DECISIONS_PATH", "/proc/nope/s.json")
    assert client.post(url, json=body, headers=auth).status_code == 503
