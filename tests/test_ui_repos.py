from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import ui_repos
from auditor.agent_pack import AgentCodingPack, rebuild_index, update_pack_status, write_pack
from auditor.config import RepoConfig
from auditor.contracts import RuleViolation
from auditor.db import AuditRun, FindingRecord, RepoRun

NOW = datetime.now(timezone.utc)


def _pack(pack_id, *, repo="argus-cortex", severity="MEDIUM", score=0.5, kind="nightly", status="open", title=None):
    return AgentCodingPack(
        work_item_id=pack_id, kind=kind,
        priority={"severity": severity, "score": score},
        repo={"name": repo, "path": "", "base_branch": "dev"},
        location={"file_path": f"/srv/mirrors/{repo}/src/{pack_id}.py", "line": 1, "symbol": "", "code_snippet": "", "evidence": ""},
        narrative={"title": title or f"title {pack_id}", "description": "d", "recommendation": "r", "acceptance_criteria": ["a"]},
        impact={"impacted_repos": [] if kind == "world_model" else [repo]},
        verification={"suggested_test_commands": []},
        status=status,
    )


@pytest.fixture
def packs(tmp_path: Path) -> Path:
    for p in (
        _pack("pack-low", severity="LOW", score=0.3),
        _pack("pack-high-a", severity="HIGH", score=0.8),
        _pack("pack-high-b", severity="HIGH", score=0.9),
        _pack("pack-crit", severity="CRITICAL", score=0.1, repo="argus-studio"),
        _pack("pack-claimed", severity="MEDIUM", status="claimed"),
        _pack("pack-world-argus-cortex", kind="world_model", severity="INFO", score=1.0),
    ):
        write_pack(p, tmp_path)
    rebuild_index(tmp_path)
    return tmp_path


# ── packs board ──────────────────────────────────────────────────────────────

def test_board_groups_by_status_most_severe_then_score(packs: Path) -> None:
    b = ui_repos.packs_board(packs)
    cols = {c["status"]: c for c in b["columns"]}
    assert [c["status"] for c in b["columns"]] == ["open", "claimed", "in_progress", "resolved", "wontfix"]
    assert [p["id"] for p in cols["open"]["packs"]] == [
        "pack-crit", "pack-high-b", "pack-high-a", "pack-low", "pack-world-argus-cortex"]
    assert cols["claimed"]["total"] == 1 and cols["resolved"]["total"] == 0
    assert cols["open"]["packs"][0]["path"] == "src/pack-crit.py"  # repo-relative
    assert b["facets"]["repos"] == ["argus-cortex", "argus-studio"]
    assert b["facets"]["layers"] == ["specialist", "world_model"]


def test_board_filters_and_column_cap(packs: Path) -> None:
    def ids(**kw):
        return [p["id"] for c in ui_repos.packs_board(packs, **kw)["columns"] for p in c["packs"]]

    assert ids(severity="critical,high") == ["pack-crit", "pack-high-b", "pack-high-a"]
    assert ids(repo="argus-studio") == ["pack-crit"]
    assert ids(layer="world_model") == ["pack-world-argus-cortex"]
    assert ids(q="HIGH-A") == ["pack-high-a"]
    capped = ui_repos.packs_board(packs, per_column=2)
    assert capped["columns"][0]["total"] == 5 and len(capped["columns"][0]["packs"]) == 2
    assert capped["total"] == 6


def test_board_without_index_is_empty(tmp_path: Path) -> None:
    b = ui_repos.packs_board(tmp_path)
    assert b["total"] == 0 and all(c["total"] == 0 for c in b["columns"])


def test_index_entries_carry_status_time_and_draft_pr(tmp_path: Path) -> None:
    p = _pack("pack-pr")
    p.repair["draft_pr_url"] = "https://github.com/o/r/pull/9"
    write_pack(p, tmp_path)
    update_pack_status(tmp_path, "pack-pr", "in_progress", "agent B")
    entry = json.loads((tmp_path / "agent_packs" / "INDEX.json").read_text())["packs"][0]
    assert entry["draft_pr_url"] == "https://github.com/o/r/pull/9"
    assert entry["status"] == "in_progress" and entry["status_updated_at"]


# ── pack page ────────────────────────────────────────────────────────────────

def test_pack_page_renders_brief_and_embeds_safe_json(packs: Path) -> None:
    update_pack_status(packs, "pack-low", "wontfix", "</script><script>alert(1)</script> <!-- x")
    page = ui_repos.render_pack_page(packs, "pack-low")
    assert "<h1>title pack-low</h1>" in page and page.count("<h1") == 1
    assert 'aria-current="page">Packs</a>' in page
    data = page.split('<script id="pack-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert "<" not in data  # nothing in the JSON can end the script block
    meta = json.loads(data)
    assert meta["status"] == "wontfix" and meta["history"][-1]["note"].startswith("</script>")
    assert ui_repos.render_pack_page(packs, "pack-nope") is None


# ── violations ───────────────────────────────────────────────────────────────

def test_reporter_writes_violations_sidecar(tmp_path: Path) -> None:
    from auditor.reporter import write_architecture_violations

    write_architecture_violations([
        RuleViolation("ui_in_worker", "agent-composer: app.py:4", ["agent-composer"], "e", "MEDIUM"),
        RuleViolation("call_graph_violation", "argus-cortex → svc", ["argus-cortex", "svc"], "e"),
    ], output_dir=str(tmp_path))
    side = json.loads((tmp_path / "architecture_violations.json").read_text())
    assert side["total"] == 2
    assert [v["repo"] for v in side["violations"]] == ["agent-composer", "argus-cortex"]
    assert ui_repos._violations_by_repo(tmp_path, []) == {"agent-composer": 1, "argus-cortex": 1}


def test_violations_fall_back_to_markdown_headings(tmp_path: Path) -> None:
    (tmp_path / "architecture_violations.md").write_text(
        "# Architecture Violations\n\n## UI in Worker Violations\n\n"
        "### argus-cortex: a.py:1\n- x\n\n### argus-cortex-extra: b.py:2\n\n"
        "### argus contains 'auth' logic (cluster: c1)\n\n### argus-studio → gateway\n")
    counts = ui_repos._violations_by_repo(tmp_path, ["argus", "argus-cortex", "argus-cortex-extra", "argus-studio"])
    assert counts == {"argus-cortex": 1, "argus-cortex-extra": 1, "argus": 1, "argus-studio": 1}
    assert ui_repos._violations_by_repo(tmp_path / "missing", ["x"]) is None


# ── repo cards ───────────────────────────────────────────────────────────────

def _git_repo(path: Path) -> str:
    path.mkdir()
    env = {"GIT_AUTHOR_DATE": "2026-11-10T12:00:00+00:00", "GIT_COMMITTER_DATE": "2026-11-10T12:00:00+00:00",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "c"], check=True, env=env)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def test_repos_data_builds_one_card_per_enabled_repo(packs: Path, memdb, monkeypatch) -> None:
    sha = _git_repo(packs / "argus-cortex")

    class _Cfg:
        repos = [RepoConfig("argus-cortex", str(packs / "argus-cortex")),
                 RepoConfig("argus-studio", str(packs / "nowhere")),
                 RepoConfig("disabled", "/x", enabled=False)]

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.delenv("AI_AUDIT_MERGE_POLL_STATE", raising=False)
    (packs / "merge_poll_state.json").write_text(json.dumps({
        "branch": "dev", "updated_at": "2026-11-14T04:00:00+00:00",
        "repos": {"argus-cortex": {"branch": "dev", "sha": sha, "slug": "o/argus-cortex", "checked_at": "2026-11-14T04:00:00+00:00"},
                  "argus-studio": {"branch": "dev", "sha": "deadbeef", "slug": "o/argus-studio", "checked_at": "2026-11-14T04:00:00+00:00"}}}))
    (packs / "architecture_violations.json").write_text(json.dumps({"violations": [{"repo": "argus-studio"}] * 2}))
    with memdb() as s:
        # distinct wording, so the reworded-finding pass can't pair them
        names = {"a": "token leaks into logs", "b": "retry loop never backs off",
                 "c": "path traversal in export", "d": "cache key ignores locale"}
        for run_id, hours, titles in (("r-old", 30, ["a", "b"]), ("r-new", 2, ["a", "c", "d"])):
            s.add(AuditRun(run_id=run_id, pipeline="repo-audit", status="success",
                           started_at=NOW - timedelta(hours=hours), metadata_json={"scanned_repos": ["argus-cortex"]}))
            s.add(RepoRun(run_id=run_id, repo_name="argus-cortex", status="completed", attempts=1,
                          error_message="", updated_at=NOW - timedelta(hours=hours)))
            for t in titles:
                s.add(FindingRecord(run_id=run_id, repo="argus-cortex", file_path="f.py", line=1, finding_type="bug",
                                    severity="HIGH" if t == "c" else "LOW", title=names[t],
                                    source="llm", created_at=NOW))

    d = ui_repos.repos_data(packs)
    assert d["errors"] == []
    assert [c["name"] for c in d["repos"]] == ["argus-cortex", "argus-studio"]
    cortex, studio = d["repos"]
    assert cortex["github_url"] == "https://github.com/o/argus-cortex"
    assert cortex["tip"]["sha"] == sha and cortex["tip"]["committed_at"].startswith("2026-11-10T12:00:00")
    assert studio["tip"]["committed_at"] is None  # mirror missing: fall back to "seen"
    assert cortex["tip"]["mirror_sha"] == sha and cortex["tip"]["mirror_behind"] is False
    assert studio["tip"]["mirror_sha"] is None and studio["tip"]["mirror_behind"] is False
    assert cortex["last_audit"]["run_id"] == "r-new" and cortex["last_audit"]["pipeline"] == "repo-audit"
    assert cortex["findings"] == {"by_severity": {"LOW": 2, "HIGH": 1}, "total": 3, "new": 2}
    assert studio["findings"] == {"by_severity": {}, "total": 0, "new": 0}
    # world-model packs aren't work; claimed counts separately
    assert cortex["packs"] == {"open": 3, "claimed": 1, "in_progress": 0, "resolved": 0, "wontfix": 0, "open_high": 2}
    assert studio["packs"]["open_high"] == 1
    assert cortex["violations"] == 0 and studio["violations"] == 2
    assert d["findings_run"]["run_id"] == "r-new" and d["findings_has_baseline"] is True


def test_repos_data_degrades_per_source(tmp_path: Path, monkeypatch) -> None:
    class _Cfg:
        repos = [RepoConfig("argus-cortex", "/nowhere")]

    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setattr("auditor.ui._db_session", broken)
    d = ui_repos.repos_data(tmp_path)
    card = d["repos"][0]
    assert card["tip"] is None and card["last_audit"] is None and card["findings"] is None
    assert card["violations"] is None
    assert [e.split(":")[0] for e in d["errors"]] == ["audits", "findings"]


# ── routes ───────────────────────────────────────────────────────────────────

def test_pages_and_json_routes(client: TestClient, packs: Path) -> None:
    for path, active in (("/ui/repos", "Repos"), ("/ui/packs", "Packs"), ("/ui/packs/pack-low", "Packs")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert f'aria-current="page">{active}</a>' in r.text
    assert client.get("/ui/packs/pack-nope").status_code == 404
    b = client.get("/audit/packs/board.json?severity=HIGH")  # not swallowed by /audit/packs/{pack_id}
    assert b.status_code == 200 and b.json()["total"] == 2


def test_status_change_accepts_the_headers_the_ui_sends(client: TestClient, packs: Path, monkeypatch) -> None:
    """ui.js sends the secret as both X-API-Token and X-Auth-Code; the API takes either."""
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "s3cret")
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    url = "/audit/packs/pack-low/status"
    assert client.post(url, json={"status": "claimed"}).status_code == 401
    assert client.post(url, json={"status": "claimed"}, headers={"X-API-Token": "nope", "X-Auth-Code": "nope"}).status_code == 401
    r = client.post(url, json={"status": "claimed", "note": "board"}, headers={"X-API-Token": "s3cret", "X-Auth-Code": "s3cret"})
    assert r.status_code == 200 and r.json()["status"] == "claimed"
