from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from auditor.agent_pack import (
    build_pack_from_finding,
    build_pack_from_worker_finding,
    emit_packs_from_worker_results,
    list_packs,
    load_pack,
    rebuild_index,
    stable_pack_id,
    write_pack,
)
from auditor.audit_api import app
from auditor.contracts import Finding
from auditor.validator import suggest_test_commands
from conductor.missions import WorkerResult, WorkerTask


def test_stable_pack_id_deterministic():
    a = stable_pack_id(repo="argus-cortex", file_path="a.py", title="t", source="s", run_id="r1")
    b = stable_pack_id(repo="argus-cortex", file_path="a.py", title="t", source="s", run_id="r1")
    assert a == b
    assert a.startswith("pack-")


def test_suggest_test_commands_python(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    tool, cmds = suggest_test_commands(tmp_path, "pkg/mod.py")
    assert tool == "pytest"
    assert any("pkg/mod.py" in c for c in cmds)


def test_write_list_load_pack(tmp_path: Path):
    src = tmp_path / "repo"
    src.mkdir()
    (src / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    target = src / "mod.py"
    target.write_text("def boom():\n    return 1\n", encoding="utf-8")

    finding = Finding(
        id="",
        type="security",
        severity="HIGH",
        repo="demo",
        file_path="mod.py",
        line=1,
        title="Example issue",
        description="desc",
        evidence="ev",
        recommendation="fix it",
        source="test",
    )
    pack = build_pack_from_finding(
        finding,
        repo_path=src,
        kind="security",
        run_id="run-1",
        public_base_url="http://example.test:8765",
    )
    path = write_pack(pack, tmp_path)
    assert path.exists()
    assert path.with_suffix(".md").exists()
    idx = rebuild_index(tmp_path, public_base_url="http://example.test:8765")
    assert idx["count"] == 1
    entries = list_packs(tmp_path, repo="demo", severity="HIGH", q="Example")
    assert len(entries) == 1
    assert entries[0]["id"] == pack.work_item_id
    loaded = load_pack(tmp_path, pack.work_item_id)
    assert loaded is not None
    assert loaded["narrative"]["title"] == "Example issue"
    assert "boom" in (loaded["location"].get("code_snippet") or "")


def test_emit_from_worker_and_api(tmp_path: Path, monkeypatch):
    repo = tmp_path / "argus-cortex"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='argus-cortex'\n", encoding="utf-8")
    (repo / "x.py").write_text("x = 1\n", encoding="utf-8")

    task = WorkerTask(
        id="argus-cortex-bughunt-001",
        mission="bughunt",
        title="Bug hunt",
        repo="argus-cortex",
        repo_path=str(repo),
        prompt="...",
    )
    result = WorkerResult(
        task_id=task.id,
        status="COMPLETED",
        summary="found one",
        findings=[
            {
                "category": "logic",
                "file": "x.py",
                "line": 1,
                "symbol": "x",
                "severity": "MEDIUM",
                "title": "Suspicious constant",
                "call_path": ["caller->x"],
                "missing_evidence": "",
            }
        ],
    )
    packs = emit_packs_from_worker_results(
        output_dir=tmp_path,
        run_id="rest-test",
        tasks=[task],
        results_by_id={task.id: result},
        public_base_url="http://192.168.1.198:8765",
        ingest_rag=False,
    )
    assert len(packs) == 1
    pack_id = packs[0].work_item_id

    # Worker + PR path should agree on id
    expected = stable_pack_id(
        repo="argus-cortex",
        file_path="x.py",
        title="Suspicious constant",
        source="conductor-bughunt",
        run_id="rest-test",
    )
    assert pack_id == expected

    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(tmp_path))
    client = TestClient(app)
    listed = client.get("/audit/packs", params={"repo": "argus-cortex", "q": "Suspicious"})
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] >= 1
    assert any(p["id"] == pack_id for p in body["packs"])

    got = client.get(f"/audit/packs/{pack_id}")
    assert got.status_code == 200
    assert got.json()["work_item_id"] == pack_id

    brief = client.get(f"/audit/packs/{pack_id}/brief")
    assert brief.status_code == 200
    assert "Agent Work Pack" in brief.text
    assert "Suspicious constant" in brief.text


def test_finding_from_violation_parses_call_evidence():
    from auditor.agent_pack import finding_from_violation
    from auditor.contracts import RuleViolation

    v = RuleViolation(
        rule_name="call_graph_violation",
        violating_path="argus-studio → voluptas",
        entities=["argus-studio", "voluptas"],
        evidence="Unauthorised direct call. Locations: client.py:42 (httpx.post())",
        severity="HIGH",
    )
    f = finding_from_violation(v)
    assert f.repo == "argus-studio"
    assert f.file_path == "client.py"
    assert f.line == 42
    assert f.severity == "HIGH"

    ui = RuleViolation(
        rule_name="ui_in_worker",
        violating_path="argus-survey: src/argus_survey/server/app.py:469",
        entities=["argus-survey"],
        evidence="UI response pattern 'HTMLResponse(' found",
        severity="MEDIUM",
    )
    f2 = finding_from_violation(ui)
    assert f2.repo == "argus-survey"
    assert f2.file_path == "src/argus_survey/server/app.py"
    assert f2.line == 469


def test_build_pack_from_worker_finding_includes_call_path(tmp_path: Path):
    pack = build_pack_from_worker_finding(
        {
            "file": "a.py",
            "line": 2,
            "title": "t",
            "severity": "HIGH",
            "call_path": ["a", "b"],
            "symbol": "fn",
        },
        repo="r",
        repo_path=tmp_path,
        mission="security",
        run_id="run",
        task_id="task",
    )
    assert pack.impact["call_path"] == ["a", "b"]
    assert pack.location["symbol"] == "fn"
    assert pack.priority["severity"] == "HIGH"
