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
