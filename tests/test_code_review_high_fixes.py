"""Regression tests for high-severity night-conductor review fixes."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from auditor.audit_api import app
from auditor.git_ops import commit_repair, is_git_repo
from conductor.mirrors import checkout_scan_branch
from conductor.rest_window import RestWindowConfig, should_freeze_launches
from conductor.session import run_rest_session


def _git_init_with_commit(repo: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    (repo / "README").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


def test_commit_repair_accepts_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_with_commit(repo)
    (repo / "intent.md").write_text("# intent\n", encoding="utf-8")
    result = commit_repair(repo, message="audit: test intent", paths=["intent.md"])
    assert result.files_staged == 1
    assert result.sha
    assert is_git_repo(repo)


def test_commit_repair_rejects_path_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_with_commit(repo)
    outside = tmp_path / "secret.txt"
    outside.write_text("nope\n", encoding="utf-8")
    with pytest.raises(Exception, match="outside repo"):
        commit_repair(repo, message="bad", paths=["../secret.txt"])


def test_rest_session_aborts_outside_window(monkeypatch, tmp_path: Path) -> None:
    cfg = RestWindowConfig(
        timezone="UTC",
        start=time(0, 30),
        end=time(6, 0),
        wrap_up_before_end_minutes=20,
        inflight_grace_s=120,
        max_tasks_per_session=12,
    )
    tz = ZoneInfo("UTC")
    noon = datetime(2026, 11, 11, 12, 0, tzinfo=tz)
    assert should_freeze_launches(cfg, noon)

    class _Eco:
        output_dir = str(tmp_path)
        repos = []

    monkeypatch.setattr("conductor.session.load_ecosystem_config", lambda: _Eco())
    monkeypatch.setattr("conductor.session.load_rest_windows", lambda: cfg)
    monkeypatch.setattr(
        "conductor.session.should_freeze_launches",
        lambda _rest: True,
    )
    called = {"sync": False}

    def _boom(*_a, **_k):
        called["sync"] = True
        raise AssertionError("mirrors must not sync when outside window")

    monkeypatch.setattr("conductor.session.sync_mirrors", _boom)
    out = run_rest_session(dry_run=False, force=False, output_dir=str(tmp_path))
    assert called["sync"] is False
    assert out["status"]["ended_reason"] == "time_budget"
    assert any("outside launch window" in d.get("summary", "") for d in out["status"]["decisions_needed"])


def test_checkout_scan_branch_ff_only_preserves_local_commit(tmp_path: Path) -> None:
    import subprocess

    def _run(args: list[str], cwd: Path) -> None:
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    work = tmp_path / "work"
    work.mkdir()
    _git_init_with_commit(work)
    _run(["git", "branch", "-M", "dev"], work)
    _run(["git", "remote", "add", "origin", str(remote)], work)
    _run(["git", "push", "-u", "origin", "dev"], work)

    # Divergent local commit not on origin.
    (work / "local-only.txt").write_text("keep me\n", encoding="utf-8")
    _run(["git", "add", "local-only.txt"], work)
    _run(["git", "commit", "-m", "local"], work)
    local_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Advance origin/dev from a second clone so work and origin diverge.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    _run(["git", "config", "user.email", "t@example.com"], other)
    _run(["git", "config", "user.name", "t"], other)
    _run(["git", "checkout", "dev"], other)
    (other / "remote-only.txt").write_text("remote\n", encoding="utf-8")
    _run(["git", "add", "remote-only.txt"], other)
    _run(["git", "commit", "-m", "remote"], other)
    _run(["git", "push", "origin", "dev"], other)

    checked = checkout_scan_branch(work, preferred="dev")
    assert checked == "dev"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == local_sha
    assert (work / "local-only.txt").exists()


def test_mutating_api_requires_token_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "secret-token")
    monkeypatch.setenv("AI_AUDIT_AUTH_MODE", "false")

    class _Eco:
        auth_mode = False
        auth_code_env = "AI_AUDIT_AUTH_CODE"
        repos = []
        output_dir = "reports"

    monkeypatch.setattr("auditor.audit_api.load_ecosystem_config", lambda: _Eco())
    client = TestClient(app)
    denied = client.post("/audit/git/push", json={"repo": "nope"})
    assert denied.status_code == 401
    allowed = client.post(
        "/audit/git/push",
        json={"repo": "nope"},
        headers={"X-API-Token": "secret-token"},
    )
    # Auth passes; repo missing → 404 rather than 401.
    assert allowed.status_code == 404
    monkeypatch.delenv("AI_AUDIT_API_TOKEN", raising=False)
