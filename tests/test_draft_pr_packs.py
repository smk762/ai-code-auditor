"""conductor.draft_pr: PR targets base_branch; the repo's own intent.md is never overwritten."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import conductor.draft_pr as dp
from auditor.git_ops import PushResult


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "intent.md").write_text("the project's own intent\n", encoding="utf-8")
    _git(r, "add", "intent.md")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "branch", "dev")
    return r


def test_draft_pr_targets_base_and_keeps_existing_intent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh_calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        if args and args[0] == "gh":
            gh_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "https://github.com/o/r/pull/7\n", "")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(dp, "_gh_available", lambda: True)
    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    monkeypatch.setattr(dp, "push_repair_branch", lambda *a, **kw: PushResult(
        pushed=True, remote="origin", branch=kw.get("branch", ""), remote_url="x"))

    res = dp.create_draft_pr(repo, repo_name="r", title="Fix it", body="notes", slug="fix it",
                             base_branch="dev")
    assert res.error == "", res.error
    assert res.draft is not None and res.draft.url.endswith("/pull/7")
    create = gh_calls[-1]
    assert create[create.index("--base") + 1] == "dev"
    assert (repo / "intent.md").read_text(encoding="utf-8") == "the project's own intent\n"
    changed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert "intent.md" not in changed
    assert changed and changed[0].endswith("intent.md")


def _gh(monkeypatch: pytest.MonkeyPatch, gh: Any, pushed: bool = True, reason: str = "") -> list[dict]:
    calls: list[dict] = []
    real_run = subprocess.run

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        if args and args[0] == "gh":
            calls.append(kw)
            return gh(args, **kw)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(dp, "_gh_available", lambda: True)
    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    monkeypatch.setattr(dp, "push_repair_branch", lambda *a, **kw: PushResult(
        pushed=pushed, remote="origin", branch=kw.get("branch", ""), remote_url="x", skipped_reason=reason))
    return calls


def test_empty_gh_stdout_is_an_error_not_a_crash(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _gh(monkeypatch, lambda args, **kw: subprocess.CompletedProcess(args, 0, "", ""))
    res = dp.create_draft_pr(repo, repo_name="r", title="Fix it", body="notes", slug="fix it")
    assert res.draft is None and res.error


def test_skipped_push_stops_before_gh(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _gh(monkeypatch, lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "no such branch"),
                pushed=False, reason="no remote 'origin' configured")
    res = dp.create_draft_pr(repo, repo_name="r", title="Fix it", body="notes", slug="fix it")
    assert calls == []
    assert res.draft is None and "no remote 'origin' configured" in res.error and res.branch


def test_gh_has_a_timeout_and_no_prompt(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(args: Any, **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(args, kw.get("timeout") or 0)

    calls = _gh(monkeypatch, hang)
    res = dp.create_draft_pr(repo, repo_name="r", title="Fix it", body="notes", slug="fix it")
    assert res.draft is None and "timed out" in res.error
    assert calls[0].get("timeout") and calls[0]["env"]["GH_PROMPT_DISABLED"] == "1"
