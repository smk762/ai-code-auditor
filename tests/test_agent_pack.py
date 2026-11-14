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


def test_emit_world_model_and_extraction_layers(tmp_path: Path, monkeypatch):
    from auditor.agent_pack import (
        emit_packs_from_extraction_candidates,
        emit_world_model_packs,
        list_packs,
        rebuild_index,
    )

    # Minimal ecosystem config stub via monkeypatch of loaders used inside emit_world_model
    mem = tmp_path / "architecture_memory.md"
    mem.write_text("# Decision\nArgus only.\n", encoding="utf-8")

    class _Repo:
        name = "argus-cortex"
        enabled = True
        branch = "dev"
        path = str(tmp_path / "argus-cortex")

    (tmp_path / "argus-cortex").mkdir()
    (tmp_path / "argus-cortex" / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    class _Cfg:
        output_dir = str(tmp_path)
        architecture_memory_path = str(mem)
        graph_db_path = str(tmp_path / "g.db")
        repos = [_Repo()]

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setattr(
        "auditor.repo_resolver.resolve_repo_path",
        lambda repo: Path(repo.path),
    )
    monkeypatch.setattr(
        "auditor.ecosystem_briefing.build_ecosystem_overview",
        lambda max_chars=8000: "overview text",
    )
    monkeypatch.setattr(
        "auditor.ecosystem_briefing.build_repo_briefing",
        lambda *a, **k: "repo briefing",
    )
    monkeypatch.setattr("auditor.ecosystem_briefing._get_repo_roles", lambda: {"argus-cortex": {"label": "Taxonomy", "role": "foundation"}})

    # GraphBuilder may touch sqlite — use real path under tmp
    world = emit_world_model_packs(
        output_dir=tmp_path,
        run_id="t",
        public_base_url="http://example.test:8765",
        include_repos=True,
    )
    assert any(p.work_item_id == "pack-world-ecosystem" for p in world)
    assert any(p.work_item_id == "pack-world-architecture-memory" for p in world)
    assert any(p.work_item_id == "pack-world-argus-cortex" for p in world)

    ext = emit_packs_from_extraction_candidates(
        [
            {
                "id": "ext-demo-001",
                "title": "Extract helper",
                "extraction_score": 0.8,
                "repos_involved": ["argus-cortex", "argus-studio"],
                "source_units": [{"repo": "argus-cortex", "path": "a.py", "symbols": ["helper"]}],
                "rationale": ["duped"],
                "migration_plan": {"phase_1": "wrap"},
                "risks": ["low"],
                "proposed_library": {"name": "shared-helper", "public_api": ["helper()"]},
            }
        ],
        output_dir=tmp_path,
        public_base_url="http://example.test:8765",
    )
    assert len(ext) == 1
    assert ext[0].kind == "extraction"
    idx = rebuild_index(tmp_path, public_base_url="http://example.test:8765")
    assert "world_model" in idx["layers"]
    assert "specialist" in idx["layers"]
    assert list_packs(tmp_path, layer="world_model")
    assert list_packs(tmp_path, kind="extraction")


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


# ── Bundle / status lifecycle / closure ───────────────────────────────────────


def _mk_pack(
    pack_id: str,
    *,
    repo: str = "argus-cortex",
    kind: str = "security",
    severity: str = "HIGH",
    score: float | None = None,
    title: str = "",
    description: str = "",
    impacted: list[str] | None = None,
    status: str = "open",
):
    from auditor.agent_pack import AgentCodingPack

    world = kind == "world_model"
    return AgentCodingPack(
        work_item_id=pack_id,
        kind=kind,
        priority={
            "severity": severity,
            "score": score if score is not None else {"CRITICAL": 1.0, "HIGH": 0.85, "MEDIUM": 0.55, "LOW": 0.3}.get(severity, 0.1),
        },
        repo={"name": repo, "path": "", "base_branch": "dev"},
        location={"file_path": f"{pack_id}.py", "line": 1, "symbol": "", "code_snippet": "", "evidence": ""},
        narrative={
            "title": title or f"title {pack_id}",
            "description": description or "desc",
            "recommendation": "do the thing",
            "acceptance_criteria": ["a"],
        },
        impact={"impacted_repos": impacted or ([] if world else [repo])},
        verification={"suggested_test_commands": ["pytest -q"]},
        status=status,
    )


def _seed_world(tmp_path: Path, *, eco_chars: int = 50) -> None:
    write_pack(
        _mk_pack("pack-world-ecosystem", repo="ecosystem", kind="world_model", severity="INFO",
                 description="E" * eco_chars),
        tmp_path,
    )
    write_pack(
        _mk_pack("pack-world-architecture-memory", repo="ecosystem", kind="world_model",
                 severity="INFO", description="M" * eco_chars),
        tmp_path,
    )
    write_pack(
        _mk_pack("pack-world-argus-cortex", kind="world_model", severity="INFO", description="R" * eco_chars),
        tmp_path,
    )


def test_bundle_ranks_specialists_for_repo(tmp_path: Path):
    from auditor.agent_pack import build_pack_bundle

    _seed_world(tmp_path)
    write_pack(_mk_pack("pack-low", severity="LOW"), tmp_path)
    write_pack(_mk_pack("pack-high", severity="HIGH"), tmp_path)
    write_pack(_mk_pack("pack-med", severity="MEDIUM", kind="architecture"), tmp_path)
    write_pack(_mk_pack("pack-other-repo", repo="argus-studio", severity="CRITICAL"), tmp_path)
    write_pack(
        _mk_pack("pack-ext", repo="argus-studio", kind="extraction", severity="HIGH",
                 impacted=["argus-studio", "argus-cortex"]),
        tmp_path,
    )
    write_pack(_mk_pack("pack-done", severity="CRITICAL", status="resolved"), tmp_path)
    rebuild_index(tmp_path)

    bundle = build_pack_bundle(tmp_path, repo="argus-cortex", limit_specialists=10)

    ids = [s["work_item_id"] for s in bundle["specialists"]]
    # severity/score first; primary-repo packs outrank packs that merely list the repo as impacted
    assert ids == ["pack-high", "pack-med", "pack-low", "pack-ext"]
    assert "pack-other-repo" not in ids  # unrelated repo
    assert "pack-done" not in ids  # resolved is closed
    assert [w["work_item_id"] for w in bundle["world_model"]] == [
        "pack-world-ecosystem",
        "pack-world-architecture-memory",
        "pack-world-argus-cortex",
    ]
    assert bundle["truncated"] is False
    assert "pack-high" in bundle["briefs_markdown"]
    assert bundle["discovery"]["specialist"] == "/audit/packs?layer=specialist"
    assert "repo=argus-cortex" in bundle["discovery"]["bundle"]

    limited = build_pack_bundle(tmp_path, repo="argus-cortex", limit_specialists=1)
    assert [s["work_item_id"] for s in limited["specialists"]] == ["pack-high"]

    boosted = build_pack_bundle(tmp_path, repo="argus-cortex", q="pack-low", limit_specialists=1)
    assert boosted["specialists"][0]["work_item_id"] == "pack-high"  # q boost (+0.25) can't beat +0.55 severity gap
    narrow = build_pack_bundle(tmp_path, repo="argus-cortex", q="pack-med", limit_specialists=1)
    assert narrow["specialists"][0]["work_item_id"] == "pack-high"


def test_bundle_primary_repo_beats_impacted_at_equal_severity(tmp_path: Path):
    from auditor.agent_pack import build_pack_bundle

    write_pack(_mk_pack("pack-a-impacted", repo="argus-studio", kind="extraction",
                        impacted=["argus-cortex"]), tmp_path)
    write_pack(_mk_pack("pack-z-primary"), tmp_path)
    rebuild_index(tmp_path)
    ids = [s["work_item_id"] for s in build_pack_bundle(tmp_path, repo="argus-cortex")["specialists"]]
    assert ids == ["pack-z-primary", "pack-a-impacted"]


def test_bundle_truncates_lowest_priority_first_and_keeps_ecosystem(tmp_path: Path):
    from auditor.agent_pack import CHARS_PER_TOKEN, build_pack_bundle

    _seed_world(tmp_path, eco_chars=30_000)
    big = "x" * 4000
    write_pack(_mk_pack("pack-s1", severity="CRITICAL", description=big), tmp_path)
    write_pack(_mk_pack("pack-s2", severity="HIGH", description=big), tmp_path)
    write_pack(_mk_pack("pack-s3", severity="LOW", description=big), tmp_path)
    rebuild_index(tmp_path)

    budget_tokens = 150
    bundle = build_pack_bundle(tmp_path, repo="argus-cortex", token_budget=budget_tokens, limit_specialists=3)

    assert bundle["truncated"] is True
    assert len(bundle["briefs_markdown"]) <= budget_tokens * CHARS_PER_TOKEN
    assert any(w["work_item_id"] == "pack-world-ecosystem" for w in bundle["world_model"])
    assert "pack-world-ecosystem" in bundle["briefs_markdown"]
    # dropped packs are always the lowest ranked suffix
    ranked = [s["work_item_id"] for s in bundle["specialists"]]
    assert ranked == ["pack-s1", "pack-s2", "pack-s3"]
    dropped = bundle["dropped_ids"]
    assert dropped, "tight budget should drop at least one specialist"
    assert dropped == ranked[len(ranked) - len(dropped):]
    assert bundle["included_ids"] == ranked[: len(ranked) - len(dropped)]
    assert "pack-s3" in dropped
    assert "pack-s1" in bundle["included_ids"]  # highest priority survives
    assert "x" * 100 not in bundle["briefs_markdown"].split("pack-s1")[-1][:600]  # compact form

    # A moderate budget keeps every specialist but degrades them to compact briefs.
    roomy = build_pack_bundle(tmp_path, repo="argus-cortex", token_budget=700, limit_specialists=3)
    assert roomy["dropped_ids"] == []
    assert roomy["truncated"] is True

    # Even a tiny budget never drops the ecosystem world pack.
    tiny = build_pack_bundle(tmp_path, repo="argus-cortex", token_budget=100)
    assert "pack-world-ecosystem" in tiny["briefs_markdown"]
    assert tiny["truncated"] is True


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    class _Cfg:
        output_dir = str(tmp_path)
        auth_mode = False
        auth_code_env = "AI_AUDIT_AUTH_CODE"
        repos: list = []

    monkeypatch.setattr("auditor.audit_api.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")  # never hit the network in tests
    monkeypatch.delenv("AI_AUDIT_API_TOKEN", raising=False)
    return TestClient(app)


def test_status_update_persists_in_yaml_and_index(tmp_path: Path, monkeypatch):
    write_pack(_mk_pack("pack-one"), tmp_path)
    write_pack(_mk_pack("pack-two"), tmp_path)
    rebuild_index(tmp_path)
    client = _client(tmp_path, monkeypatch)

    assert load_pack(tmp_path, "pack-one")["status"] == "open"

    resp = client.post("/audit/packs/pack-one/status", json={"status": "claimed", "note": "agent A"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "claimed"

    data = load_pack(tmp_path, "pack-one")
    assert data["status"] == "claimed"
    assert data["status_note"] == "agent A"
    assert data["status_history"][-1]["status"] == "claimed"
    entry = next(e for e in list_packs(tmp_path) if e["id"] == "pack-one")
    assert entry["status"] == "claimed"

    # filter by status via API + function
    body = client.get("/audit/packs", params={"status": "claimed"}).json()
    assert [p["id"] for p in body["packs"]] == ["pack-one"]
    assert [e["id"] for e in list_packs(tmp_path, status="open")] == ["pack-two"]
    assert body["discovery"]["specialist"] == "/audit/packs?layer=specialist"
    assert body["status_counts"] == {"claimed": 1, "open": 1}

    # invalid status rejected, unknown pack 404
    assert client.post("/audit/packs/pack-one/status", json={"status": "bogus"}).status_code == 422
    assert client.post("/audit/packs/nope/status", json={"status": "open"}).status_code == 404

    # re-emitting the pack must not reset an agent-set status
    write_pack(_mk_pack("pack-one"), tmp_path)
    assert load_pack(tmp_path, "pack-one")["status"] == "claimed"


def test_status_endpoint_requires_auth_when_token_set(tmp_path: Path, monkeypatch):
    write_pack(_mk_pack("pack-one"), tmp_path)
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "tok")

    assert client.post("/audit/packs/pack-one/status", json={"status": "claimed"}).status_code == 401
    ok = client.post(
        "/audit/packs/pack-one/status",
        json={"status": "claimed"},
        headers={"X-API-Token": "tok"},
    )
    assert ok.status_code == 200
    # reads stay open
    assert client.get("/audit/packs/bundle", params={"repo": "argus-cortex"}).status_code == 200


def test_bundle_endpoint_requires_repo(tmp_path: Path, monkeypatch):
    _seed_world(tmp_path)
    write_pack(_mk_pack("pack-high"), tmp_path)
    client = _client(tmp_path, monkeypatch)

    assert client.get("/audit/packs/bundle").status_code == 422
    body = client.get(
        "/audit/packs/bundle", params={"repo": "argus-cortex", "token_budget": 2500}
    ).json()
    assert body["repo"] == "argus-cortex"
    assert body["specialists"][0]["work_item_id"] == "pack-high"
    assert body["world_model"][0]["work_item_id"] == "pack-world-ecosystem"
    # "bundle" must not be swallowed by /audit/packs/{pack_id}
    assert "briefs_markdown" in body


def _validate_setup(tmp_path: Path, monkeypatch, *, passed: bool):
    from auditor.config import RepoConfig  # noqa: F401  (ensures module import path is valid)
    from auditor.contracts import ValidationResult

    repo_dir = tmp_path / "src" / "argus-cortex"
    repo_dir.mkdir(parents=True)
    (repo_dir / "a.txt").write_text("one\n", encoding="utf-8")

    class _RepoCfg:
        name = "argus-cortex"
        path = str(repo_dir)

    class _Cfg:
        output_dir = str(tmp_path / "reports")
        auth_mode = False
        auth_code_env = "AI_AUDIT_AUTH_CODE"
        repos = [_RepoCfg()]

    monkeypatch.setattr("auditor.audit_api.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    monkeypatch.delenv("AI_AUDIT_API_TOKEN", raising=False)
    monkeypatch.setattr("auditor.repo_resolver.resolve_repo_path", lambda repo: Path(repo.path))
    monkeypatch.setattr(
        "auditor.validator.validate_repo",
        lambda repo, path_override=None, timeout_s=120: ValidationResult(
            passed=passed, tool="pytest", duration_ms=1,
            errors=[] if passed else ["boom"], raw_output="ok" if passed else "FAILED",
        ),
    )
    return _Cfg.output_dir


PATCH = """--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-one
+two
"""


def test_validate_patch_pass_resolves_pack(tmp_path: Path, monkeypatch):
    out = _validate_setup(tmp_path, monkeypatch, passed=True)
    write_pack(_mk_pack("pack-fixme"), out)
    write_pack(_mk_pack("pack-untouched"), out)
    rebuild_index(out)
    client = TestClient(app)

    resp = client.post(
        "/audit/validate_patch",
        json={"repo": "argus-cortex", "patch": PATCH, "pack_id": "pack-fixme"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    assert body["pack"] == {"id": "pack-fixme", "updated": True, "status": "resolved"}

    data = load_pack(out, "pack-fixme")
    assert data["status"] == "resolved"
    assert "validate_patch passed" in data["status_note"]
    assert next(e for e in list_packs(out) if e["id"] == "pack-fixme")["status"] == "resolved"
    assert load_pack(out, "pack-untouched")["status"] == "open"

    # without pack_id nothing changes and no "pack" key is returned
    plain = client.post("/audit/validate_patch", json={"repo": "argus-cortex", "patch": PATCH}).json()
    assert "pack" not in plain


def test_validate_patch_failure_or_wrong_repo_leaves_pack_open(tmp_path: Path, monkeypatch):
    out = _validate_setup(tmp_path, monkeypatch, passed=False)
    write_pack(_mk_pack("pack-fixme"), out)
    write_pack(_mk_pack("pack-foreign", repo="argus-studio"), out)
    client = TestClient(app)

    failed = client.post(
        "/audit/validate_patch",
        json={"repo": "argus-cortex", "patch": PATCH, "pack_id": "pack-fixme"},
    ).json()
    assert failed["passed"] is False
    assert failed["pack"]["updated"] is False
    assert load_pack(out, "pack-fixme")["status"] == "open"

    # passing run, but pack belongs to another repo
    from auditor.contracts import ValidationResult

    monkeypatch.setattr(
        "auditor.validator.validate_repo",
        lambda repo, path_override=None, timeout_s=120: ValidationResult(
            passed=True, tool="pytest", duration_ms=1, errors=[], raw_output="ok"
        ),
    )
    foreign = client.post(
        "/audit/validate_patch",
        json={"repo": "argus-cortex", "patch": PATCH, "pack_id": "pack-foreign"},
    ).json()
    assert foreign["passed"] is True
    assert foreign["pack"]["updated"] is False
    assert load_pack(out, "pack-foreign")["status"] == "open"


def test_draft_pr_marks_pack_in_progress(tmp_path: Path):
    repo = tmp_path / "argus-cortex"
    repo.mkdir()
    task = WorkerTask(
        id="argus-cortex-bughunt-001", mission="bughunt", title="Bug hunt",
        repo="argus-cortex", repo_path=str(repo), prompt="...",
    )
    result = WorkerResult(
        task_id=task.id, status="COMPLETED", summary="s",
        findings=[
            {"category": "logic", "file": "x.py", "line": 1, "severity": "MEDIUM", "title": "First"},
            {"category": "logic", "file": "y.py", "line": 1, "severity": "MEDIUM", "title": "Second"},
        ],
    )
    first_id = stable_pack_id(repo="argus-cortex", file_path="x.py", title="First",
                              source="conductor-bughunt", run_id="r1")
    pr = "https://github.com/smk762/argus-cortex/pull/7"
    packs = emit_packs_from_worker_results(
        output_dir=tmp_path, run_id="r1", tasks=[task], results_by_id={task.id: result},
        draft_pr_by_pack={first_id: pr}, ingest_rag=False,
    )
    assert len(packs) == 2
    first = load_pack(tmp_path, first_id)
    assert first["status"] == "in_progress"
    assert first["repair"]["draft_pr_url"] == pr
    other = next(p for p in packs if p.work_item_id != first_id)
    assert load_pack(tmp_path, other.work_item_id)["status"] == "open"
    # re-emit keeps in_progress + url
    emit_packs_from_worker_results(
        output_dir=tmp_path, run_id="r1", tasks=[task], results_by_id={task.id: result}, ingest_rag=False,
    )
    again = load_pack(tmp_path, first_id)
    assert again["status"] == "in_progress"
    assert again["repair"]["draft_pr_url"] == pr


def test_pack_rag_ingest_enabled_and_metadata(monkeypatch):
    from auditor import agent_pack

    monkeypatch.delenv("AI_AUDIT_PACK_RAG_INGEST", raising=False)
    assert agent_pack.pack_rag_ingest_enabled() is True
    assert agent_pack.pack_rag_ingest_enabled(False) is False  # --no-rag
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    assert agent_pack.pack_rag_ingest_enabled() is False
    assert agent_pack.pack_rag_ingest_enabled(True) is False  # kill switch wins

    posted: list[dict] = []
    sent_headers: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

    import hashlib
    import hmac
    import json as _json
    import requests

    def fake_post(url, data, headers, timeout):
        posted.append(_json.loads(data))
        sent_headers.append(headers | {"_body": data})
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("INGEST_SHARED_SECRET", "s3cret")
    pack = _mk_pack("pack-rag", status="claimed")
    result = agent_pack.ingest_packs_to_rag([pack])
    assert result["ingested"] is True
    doc = posted[0]["docs"][0]  # rag-ingest's IngestDoc shape
    assert doc["id"] == "agent-pack::pack-rag" and "status=claimed" in doc["text"]
    assert "claimed" in doc["title"] and "specialist" in doc["title"]
    assert posted[0]["tags"] == ["kind:agent_coding_pack", f"repo:{pack.repo['name']}"]
    h = sent_headers[0]
    # verifies exactly as rag-ingest's verify_signature does: key = sha256(secret), msg = ts.nonce.sha256(body)
    key = hashlib.sha256(b"s3cret").digest()
    msg = h["X-Timestamp"].encode() + b"." + h["X-Nonce"].encode() + b"." + hashlib.sha256(h["_body"]).hexdigest().encode()
    assert hmac.compare_digest(h["X-Signature"], hmac.new(key, msg, hashlib.sha256).hexdigest())

    monkeypatch.delenv("INGEST_SHARED_SECRET")
    unsigned = agent_pack.ingest_packs_to_rag([pack])
    assert unsigned["ingested"] is False and "INGEST_SHARED_SECRET" in unsigned["error"]
    assert len(posted) == 1  # nothing sent unsigned
