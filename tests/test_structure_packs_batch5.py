"""auditor/structure_packs.py: re-deciding an older decision record, empty stored lists, and an
unreadable decisions file with no report."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor import structure
from auditor import structure_packs as sp
from auditor.decisions import load_decisions, save_decisions

BASE = {"app/__init__.py": "", "app/a.py": "from app.b import go\nLIMIT = 1\n",
        "app/b.py": "def go():\n    from app.a import LIMIT\n    return LIMIT\n"}
CID = sp.cycle_id("mono", ["app/a.py", "app/b.py"])


@pytest.fixture
def out(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    monkeypatch.setenv("AI_AUDIT_STRUCTURE_DECISIONS_PATH", str(tmp_path / "rules" / "s.json"))
    o = tmp_path / "reports"
    o.mkdir()
    (o / structure.REPORT_NAME).write_text(json.dumps({"at": "t", "repos": {"mono": structure.scan_sources(BASE)}}))
    return o


def _store(tmp_path: Path, record: dict) -> Path:
    p = tmp_path / "rules" / "s.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    save_decisions(p, {CID: {"status": "dismissed", "repo": "mono", "note": "", "decided_at": "t", **record}})
    return p


def test_redeciding_a_record_without_members_falls_back_to_the_scan(out: Path, tmp_path: Path) -> None:
    p = _store(tmp_path, {})  # an older record: no members, edges or cuts
    assert sp.decide(out, CID, "dismissed", note="still fine")["decision"] == "dismissed"
    assert load_decisions(p)[CID]["members"] == ["app/a.py", "app/b.py"]


def test_empty_stored_edges_and_cuts_are_kept(out: Path, tmp_path: Path) -> None:
    p = _store(tmp_path, {"members": ["app/a.py", "app/b.py"], "edges": [], "cuts": []})
    sp.decide(out, CID, "dismissed", note="again")
    d = load_decisions(p)[CID]
    assert d["edges"] == [] and d["cuts"] == []


def test_an_unreadable_decisions_file_is_reported_even_with_no_report(out: Path, tmp_path: Path) -> None:
    (out / structure.REPORT_NAME).unlink()
    p = tmp_path / "rules" / "s.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"decisions": [1]}')
    got = sp.ui_data(out)
    assert got is not None and got["decisions_error"] and got["cycles"] == []
    p.unlink()
    assert sp.ui_data(out) is None  # nothing at all is still "no data"
