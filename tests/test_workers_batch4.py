"""conductor.workers.run_workers_parallel: cancellation keeps finished work and gives every
task exactly one result."""

from __future__ import annotations

import threading
from typing import Any

import conductor.workers as w
from conductor.missions import WorkerResult, WorkerTask


def _task(tid: str) -> WorkerTask:
    return WorkerTask(id=tid, mission="bughunt", title=tid, repo="r", repo_path="/x", prompt="p")


def test_cancel_keeps_finished_results_and_records_cancelled_tasks(monkeypatch: Any) -> None:
    go = threading.Event()

    def worker(task: WorkerTask, *, dry_run: bool = False) -> WorkerResult:
        if task.id != "t1":
            go.wait(5)
        return WorkerResult(task_id=task.id, status="COMPLETED", summary=f"done {task.id}")

    def cancel() -> bool:
        # Let a worker that is already running finish shortly after the cancel lands.
        threading.Timer(0.3, go.set).start()
        return True

    monkeypatch.setattr(w, "run_worker", worker)
    results = w.run_workers_parallel([_task("t1"), _task("t2"), _task("t3")], max_workers=1,
                                     should_cancel=cancel)
    by_id = {r.task_id: r for r in results}

    assert sorted(r.task_id for r in results) == ["t1", "t2", "t3"]  # one result per task
    assert by_id["t1"].status == "COMPLETED"  # the worker whose completion triggered the cancel
    assert by_id["t3"].status == "FAILED" and "cancelled" in by_id["t3"].summary
    # t2 was either picked up before the cancel (kept) or never started (cancelled).
    assert by_id["t2"].status == "COMPLETED" or "cancelled" in by_id["t2"].summary


def test_running_workers_are_waited_for_and_kept(monkeypatch: Any) -> None:
    started = threading.Event()
    go = threading.Event()

    def worker(task: WorkerTask, *, dry_run: bool = False) -> WorkerResult:
        if task.id == "slow":
            started.set()
            go.wait(5)
        else:
            started.wait(5)
        return WorkerResult(task_id=task.id, status="COMPLETED", summary="ok")

    def cancel() -> bool:
        go.set()
        return True

    monkeypatch.setattr(w, "run_worker", worker)
    results = w.run_workers_parallel([_task("slow"), _task("fast")], max_workers=2, should_cancel=cancel)
    assert {r.task_id: r.status for r in results} == {"slow": "COMPLETED", "fast": "COMPLETED"}


def test_without_cancel_every_result_is_returned(monkeypatch: Any) -> None:
    monkeypatch.setattr(w, "run_worker", lambda t, dry_run=False: WorkerResult(t.id, "COMPLETED", "ok"))
    results = w.run_workers_parallel([_task("a"), _task("b")], should_cancel=lambda: False)
    assert sorted(r.task_id for r in results) == ["a", "b"]
