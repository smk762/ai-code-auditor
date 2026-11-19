"""run_rest_session bookkeeping: time budget and draft-PR links (packs 8a6d3fcf29, d89fddec08)."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

import pytest

import conductor.session as cs
from conductor.draft_pr import DraftPrResult
from conductor.github_awareness import RepoAwareness
from conductor.missions import WorkerResult, WorkerTask
from conductor.rest_window import RestWindowConfig
from conductor.scanners import ScannerResult
from conductor.schemas import DraftPr


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tasks: list[WorkerTask],
           results: list[WorkerResult], *, freeze_after_first: bool, pr_repos: set[str]) -> list[str]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    class _Repo:
        name = "a"
        path = str(repo_dir)

    class _Eco:
        output_dir = str(tmp_path / "out")
        repos = (_Repo(),)

    cfg = RestWindowConfig(timezone="UTC", start=time(0, 30), end=time(6, 0), wrap_up_before_end_minutes=20,
                           inflight_grace_s=120, max_tasks_per_session=12)
    calls = {"n": 0}

    def _freeze(_rest: Any) -> bool:
        calls["n"] += 1
        return freeze_after_first and calls["n"] > 1

    created: list[str] = []

    def _create(_path: Path, *, repo_name: str, **_k: Any) -> DraftPrResult:
        if repo_name not in pr_repos:
            return DraftPrResult(draft=None, error="")
        url = f"https://github.com/smk762/{repo_name}/pull/1"
        created.append(url)
        return DraftPrResult(draft=DraftPr(url=url, title="t", severity="HIGH", repo=repo_name))

    monkeypatch.setattr(cs, "load_ecosystem_config", lambda: _Eco())
    monkeypatch.setattr(cs, "load_rest_windows", lambda: cfg)
    monkeypatch.setattr(cs, "should_freeze_launches", _freeze)
    monkeypatch.setattr(cs, "sync_mirrors", list)
    monkeypatch.setattr(cs, "load_dispositions", dict)
    monkeypatch.setattr(cs, "fetch_repo_awareness", lambda _p, repo_slug="": RepoAwareness(repo="a"))
    monkeypatch.setattr(cs, "run_scanners", lambda p, o: ScannerResult(Path(o), {}, [], "", {}))
    monkeypatch.setattr(cs, "filter_by_dispositions", lambda f, _d: f)
    monkeypatch.setattr(cs, "filter_findings_against_awareness", lambda f, _a: (f, []))
    monkeypatch.setattr(cs, "build_tasks_from_scanners", lambda **_k: list(tasks))
    monkeypatch.setattr(cs, "run_workers_parallel", lambda *_a, **_k: list(results))
    monkeypatch.setattr(cs, "create_draft_pr", _create)
    monkeypatch.setattr(cs, "_refresh_pr_intel", lambda _o: None)
    monkeypatch.setattr(cs, "emit_packs_from_worker_results", lambda **_k: [])
    monkeypatch.setattr(cs, "wrap_up", lambda status, **_k: {"status": status.to_dict()})
    return created


def _task(tid: str, repo: str, tmp_path: Path) -> WorkerTask:
    return WorkerTask(id=tid, mission="security_review", title=tid, repo=repo,
                      repo_path=str(tmp_path / "repo"), prompt="p")


def _found(tid: str, title: str, status: str = "COMPLETED", reason: str = "") -> WorkerResult:
    return WorkerResult(task_id=tid, status=status, summary="s", escalate_reason=reason,
                        findings=[{"title": title, "file": "x.py", "severity": "HIGH"}])


def test_time_budget_keeps_bookkeeping_for_finished_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tasks = [_task("t1", "a", tmp_path), _task("t2", "a", tmp_path)]
    results = [_found("t1", "first bug"), _found("t2", "needs a human", status="ESCALATE", reason="ask owner")]
    created = _setup(monkeypatch, tmp_path, tasks, results, freeze_after_first=True, pr_repos={"a"})

    out = cs.run_rest_session(enable_pr_review=False, send_webhook=False, ingest_rag=False)
    status = out["status"]

    assert created == []  # out of time: no new draft PRs
    assert status["worker_stats"]["tasks"] == 2
    assert status["worker_stats"]["escalated"] == 1
    assert any(d.get("id") == "t2" for d in status["decisions_needed"])
    assert status["findings"]["confirmed"] == 2
    handoffs = list((tmp_path / "out" / "conductor").rglob("*"))
    assert sum(1 for h in handoffs if "t2" in h.name) >= 1


def test_top_finding_links_only_its_own_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tasks = [_task("t1", "a", tmp_path), _task("t2", "b", tmp_path)]
    results = [_found("t1", "bug in a"), _found("t2", "bug in b")]
    created = _setup(monkeypatch, tmp_path, tasks, results, freeze_after_first=False, pr_repos={"b"})

    out = cs.run_rest_session(force=True, enable_pr_review=False, send_webhook=False, ingest_rag=False)
    top = {t["title"]: t for t in out["status"]["findings"]["top"]}

    assert created == ["https://github.com/smk762/b/pull/1"]
    assert "pr_url" not in top["bug in a"]
    assert top["bug in b"]["pr_url"] == created[0]


def test_dry_run_attaches_no_placeholder_pr_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tasks = [_task("t1", "a", tmp_path)]
    results = [_found("t1", "bug in a")]
    _setup(monkeypatch, tmp_path, tasks, results, freeze_after_first=False, pr_repos={"a"})

    out = cs.run_rest_session(dry_run=True, force=True, enable_pr_review=False, send_webhook=False,
                              ingest_rag=False)
    assert all("pr_url" not in t for t in out["status"]["findings"]["top"])
