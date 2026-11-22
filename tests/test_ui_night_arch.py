from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from auditor import ui_arch, ui_night


# ── night summary ────────────────────────────────────────────────────────────

def _mission(d: Path, task_id: str, mission: str, status: str, summary: str = "", reason: str = "", findings=()):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.json").write_text(json.dumps({
        "task": {"id": task_id, "mission": mission, "title": f"{mission} on x"},
        "result": {"task_id": task_id, "status": status, "summary": summary, "escalate_reason": reason,
                   "findings": list(findings), "patch_notes": ""}}))


@pytest.fixture
def nights(tmp_path: Path) -> Path:
    c = tmp_path / "conductor"
    old, new = c / "rest-20261112T163016-f89040", c / "rest-20261113T163007-ab44e6"
    for repo in ("argus-ci", "argus-cull"):
        _mission(old, f"{repo}-bughunt-001", "bughunt", "FAILED", "ANTHROPIC_API_KEY required", "ANTHROPIC_API_KEY required")
    _mission(new, "argus-ci-bughunt-001", "bughunt", "PARTIAL",
             findings=[{"id": "F1", "title": "race in poller", "severity": "HIGH", "recommendation": "lock it"}])
    _mission(new, "argus-curator-bughunt-001", "bughunt", "PARTIAL",
             "No findings reported. The repository at /mirrors/argus-curator was not available")
    _mission(new, "argus-cull-invariants-001", "invariants", "COMPLETED")
    (c / "not-a-session").mkdir()
    m = tmp_path / "mithrandir"
    (m / "archive").mkdir(parents=True)
    rec = {"run_id": "rest-20261113T163007-ab44e6", "summary_one_liner": "Rest completed: 24 confirmed",
           "ended_reason": "completed", "window": {"start": "00:30", "end": "06:00", "timezone": "Australia/Perth"},
           "findings": {"confirmed": 24, "dismissed": 4, "manual_review": 2, "top": []},
           "draft_prs": [], "decisions_needed": [{"id": "pr-argus-ci-invariants-001", "repo": "argus-ci",
                                                  "summary": "Draft PR failed: Resource not accessible by personal access token\n"}],
           "worker_stats": {"tasks": 3, "completed": 0, "failed": 0}, "cost_usd_est": 0.0}
    (m / "LATEST.json").write_text(json.dumps(rec))
    (m / "archive" / "2026-11-12T16-31.json").write_text(json.dumps({"run_id": "rest-20261112T163016-f89040",
                                                                       "summary_one_liner": "Rest completed: 0 confirmed"}))
    return tmp_path


def test_night_takes_outcomes_from_mission_files(nights: Path) -> None:
    from auditor.spend_ledger import get_ledger

    led = get_ledger()
    led.record("conductor", "claude-opus-5-5", 100_000, 10_000, note="bughunt:argus-ci")
    # rows are timestamped now; move them into the newest session's window
    start = datetime(2026, 11, 13, 16, 30, 7, tzinfo=timezone.utc).timestamp()
    with led._lock:
        led._conn.execute("UPDATE spend SET ts = ?", (start + 600,))
        led._conn.commit()

    d = ui_night.night_data(nights)
    s = d["session"]
    assert s["run_id"] == "rest-20261113T163007-ab44e6"  # newest by default
    assert s["missions_total"] == 3
    assert s["mission_counts"] == {"COMPLETED": 1, "PARTIAL": 2, "FAILED": 0, "ESCALATED": 0}
    assert {m["repo"] for m in s["missions"]} == {"argus-ci", "argus-curator", "argus-cull"}
    assert s["findings_reported"] == 1 and s["confirmed"] == 24
    assert s["headline"] == "Rest completed: 24 confirmed"
    assert s["spend"]["calls"] == 1 and s["spend"]["usd"] > 0  # from the ledger, not cost_usd_est
    kinds = [p["kind"] for p in s["problems"]]
    texts = [p["text"] for p in s["problems"]]
    assert kinds == ["mission", "decision"]
    assert texts[0].startswith("incomplete: No findings reported. The repository at /mirrors/argus-curator")
    assert texts[1] == "Draft PR failed: Resource not accessible by personal access token"
    assert s["problems"][0]["missions"] == ["argus-curator-bughunt-001"]
    assert [h["run_id"] for h in d["history"]] == ["rest-20261113T163007-ab44e6", "rest-20261112T163016-f89040"]
    assert d["history"][1]["spend"]["calls"] == 0  # its window ends where the next session starts


def test_night_groups_identical_failures(nights: Path) -> None:
    s = ui_night.night_data(nights, "rest-20261112T163016-f89040")["session"]
    assert s["mission_counts"]["FAILED"] == 2
    assert s["problems"] == [{"kind": "mission", "text": "failed: ANTHROPIC_API_KEY required",
                              "missions": ["argus-ci-bughunt-001", "argus-cull-bughunt-001"]}]
    assert s["headline"] == "Rest completed: 0 confirmed"  # from the archive


def test_night_unknown_and_empty(nights: Path, tmp_path_factory) -> None:
    assert ui_night.night_data(nights, "rest-nope") is None
    empty = ui_night.night_data(tmp_path_factory.mktemp("empty"))
    assert empty == {"session": None, "history": []}


# ── architecture & patterns ──────────────────────────────────────────────────

CANDIDATE = {"id": "ext-retr-001", "title": "Extract retry into retry-core", "domain": "retry", "priority": "high",
             "extraction_score": 0.82, "repos_involved": ["argus-cortex", "argus-studio"],
             "source_units": [{"repo": "argus-cortex", "path": "/m/argus-cortex/a.py", "symbols": ["retry"]},
                              {"repo": "argus-studio", "path": "/m/argus-studio/src/s.py", "symbols": ["retry"]}],
             "proposed_library": {"name": "retry-core", "owner_repo": "argus-cortex"},
             "rationale": "same backoff in two repos", "risks": ["signature drift"]}

RULES = """rules:
  - name: service_boundary
    description: no direct DB access
    forbidden_edges:
      - "repo::gateway->repo::database:DEPENDS_ON"

# Service roles for the suite.
service_roles:
  - name: argus-cortex
    role: worker
"""


@pytest.fixture
def arch(tmp_path: Path) -> Path:
    (tmp_path / "extraction_candidates.yaml").write_text(yaml.safe_dump({"generated_at": "2026-11-13T18:00:16+00:00",
                                                                         "candidates": [CANDIDATE]}))
    (tmp_path / "pattern_proposals.md").write_text(
        "# Pattern Proposals\n\n## requests\n- Status: pending\n- Domain: Imports/Dependencies\n"
        "- Recommendation: Standardize usage pattern: requests\n- Risk: Low risk\n\n"
        "## httpx\n- Status: approved\n- Domain: Imports/Dependencies\n- Recommendation: r\n- Risk: x\n")
    (tmp_path / "rules.yaml").write_text(RULES)
    return tmp_path


def test_architecture_data_reads_all_three_reports(arch: Path) -> None:
    (arch / "architecture_violations.json").write_text(json.dumps({"generated_at": "t", "violations": [
        {"rule_name": "ui_in_worker", "repo": "agent-composer", "violating_path": "agent-composer: a.py:4",
         "severity": "medium", "evidence": "HTMLResponse(", "entities": ["agent-composer"]}]}))
    d = ui_arch.architecture_data(arch, [], arch / "decisions.json")
    assert d["violations"]["source"] == "json"
    assert d["violations"]["violations"][0] == {"rule": "ui_in_worker", "repo": "agent-composer", "path": "agent-composer: a.py:4",
                                                "severity": "MEDIUM", "evidence": "HTMLResponse(", "entities": ["agent-composer"]}
    assert d["violation_counts"] == {"by_rule": {"ui_in_worker": 1}, "by_repo": {"agent-composer": 1}}
    assert [p["name"] for p in d["proposals"]] == ["requests", "httpx"]
    assert d["proposals"][0]["recommendation"] == "Standardize usage pattern: requests"
    assert d["proposal_counts"] == {"pending": 1, "approved": 1}
    c = d["extraction"]["candidates"][0]
    assert c["id"] == "ext-retr-001" and c["decision"] is None and c["pack"] is None and c["reported"]
    assert [u["key"] for u in c["units"]] == ["argus-cortex:a.py:retry", "argus-studio:src/s.py:retry"]


def test_violations_markdown_fallback(arch: Path) -> None:
    (arch / "architecture_violations.md").write_text(
        "# Architecture Violations\n\n- Total violations: 2\n\n## UI in Worker Violations\n\n"
        "### agent-composer: rag-ingest/app/main.py:474\n- Severity: MEDIUM\n- Evidence: UI response pattern\n\n"
        "## Call Graph Violations\n\n### argus-studio → gateway\n- Severity: HIGH\n- Entities: argus-studio, gateway\n- Evidence: direct call\n")
    v = ui_arch.architecture_data(arch, ["agent-composer", "argus-studio"], arch / "decisions.json")["violations"]
    assert v["source"] == "markdown"
    assert [(x["rule"], x["repo"], x["severity"]) for x in v["violations"]] == [
        ("ui_in_worker", "agent-composer", "MEDIUM"), ("call_graph_violation", "argus-studio", "HIGH")]
    assert v["violations"][1]["entities"] == ["argus-studio", "gateway"]


def test_missing_reports_are_none(tmp_path: Path) -> None:
    d = ui_arch.architecture_data(tmp_path, [], tmp_path / "decisions.json")
    assert d["violations"] is None and d["proposals"] is None and d["extraction"] is None


# ── routes ───────────────────────────────────────────────────────────────────

def test_pages_and_json(client: TestClient, nights: Path) -> None:
    for path, active in (("/ui/night", "Night"), ("/ui/architecture", "Architecture")):
        r = client.get(path)
        assert r.status_code == 200 and f'aria-current="page">{active}</a>' in r.text
    assert client.get("/audit/night.json").json()["session"]["run_id"] == "rest-20261113T163007-ab44e6"
    assert client.get("/audit/night.json?run=rest-nope").status_code == 404
    assert client.get("/audit/architecture.json").status_code == 200


def test_decision_endpoints_need_auth_and_write_the_pack(client: TestClient, arch: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_EXTRACTION_DECISIONS_PATH", str(arch / "rules" / "extraction_decisions.json"))
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(arch))
    monkeypatch.setattr("auditor.audit_api._repo_paths", dict)
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "s3cret")
    auth = {"X-API-Token": "s3cret"}
    url = "/audit/extraction/ext-retr-001/decision"
    body = {"status": "approved", "target_repo": "argus-cortex"}
    assert client.post(url, json=body).status_code == 401
    assert client.delete(url).status_code == 401
    r = client.post(url, json=body, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["pack_id"] == "pack-ext-retr-001" and r.json()["status"] == "open"
    c = client.get("/audit/architecture.json").json()["extraction"]["candidates"][0]
    assert c["decision"]["status"] == "approved" and c["pack"]["status"] == "open"
    assert client.post("/audit/extraction/ext-nope/decision", json=body, headers=auth).status_code == 404
    assert client.post(url, json={**body, "status": "maybe"}, headers=auth).status_code == 422
    assert client.post(url, json={**body, "units": ["x:y:z"]}, headers=auth).status_code == 422
    assert client.post("/audit/extraction/a%20b/decision", json=body, headers=auth).status_code == 422
    assert client.delete(url, headers=auth).json()["pack_status"] == "obsolete"
    assert client.delete(url, headers=auth).status_code == 404


def test_an_unwritable_decisions_file_is_a_503(client: TestClient, arch: Path, monkeypatch) -> None:
    # The 500 of 2026-11-21: the rules folder wasn't writable in the container.
    monkeypatch.setenv("AI_AUDIT_EXTRACTION_DECISIONS_PATH", "/proc/nope/extraction_decisions.json")
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(arch))
    monkeypatch.setattr("auditor.audit_api._repo_paths", dict)
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "s3cret")
    r = client.post("/audit/extraction/ext-retr-001/decision", json={"status": "dismissed"},
                    headers={"X-API-Token": "s3cret"})
    assert r.status_code == 503 and "Can't write extraction decisions" in r.json()["detail"]
