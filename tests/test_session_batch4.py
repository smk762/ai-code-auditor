"""run_rest_session: a dry-run outside the window isn't cancelled, and a finding skipped for an
overlapping PR is counted as confirmed only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import conductor.session as cs
from conductor.github_awareness import RepoAwareness
from conductor.missions import WorkerResult
from tests.test_session_packs import _found, _setup, _task


def test_dry_run_outside_the_window_is_not_cancelled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tasks = [_task("t1", "a", tmp_path)]
    _setup(monkeypatch, tmp_path, tasks, [], freeze_after_first=False, pr_repos={"a"})
    monkeypatch.setattr(cs, "should_freeze_launches", lambda _rest: True)  # outside the window
    cancel_seen: list[bool] = []

    def _workers(pending: list[Any], *, should_cancel: Any = None, **_k: Any) -> list[WorkerResult]:
        cancel_seen.append(bool(should_cancel and should_cancel()))
        return [_found("t1", "bug in a")]

    monkeypatch.setattr(cs, "run_workers_parallel", _workers)
    out = cs.run_rest_session(dry_run=True, enable_pr_review=False, send_webhook=False, ingest_rag=False)
    status = out["status"]

    assert cancel_seen == [False]
    assert status["ended_reason"] != "time_budget"
    assert status["worker_stats"]["tasks"] == 1
    assert len(status["draft_prs"]) == 1  # the dry-run went through the draft-PR stage


def test_overlap_skip_is_not_also_a_dismissal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tasks = [_task("t1", "a", tmp_path)]
    created = _setup(monkeypatch, tmp_path, tasks, [_found("t1", "bug in a")],
                     freeze_after_first=False, pr_repos={"a"})
    monkeypatch.setattr(cs, "overlap_reason", lambda **_k: "open PR #7 touches x.py")
    monkeypatch.setattr(cs, "fetch_repo_awareness", lambda _p, repo_slug="": RepoAwareness(repo="a"))

    out = cs.run_rest_session(force=True, enable_pr_review=False, send_webhook=False, ingest_rag=False)
    findings = out["status"]["findings"]

    assert created == []
    assert findings["confirmed"] == 1
    assert findings["dismissed"] == 0
