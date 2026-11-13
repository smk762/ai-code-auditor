"""Opus worker dispatch — conductor sits idle while workers run."""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from conductor.llm import completion, load_conductor_models
from conductor.missions import WorkerResult, WorkerTask, parse_worker_json, system_for

logger = logging.getLogger(__name__)


def run_worker(task: WorkerTask, *, dry_run: bool = False) -> WorkerResult:
    """Run a single Opus worker for a task. Conductor must not do this work itself."""
    if dry_run:
        return WorkerResult(
            task_id=task.id,
            status="COMPLETED",
            summary=f"[dry-run] {task.mission} skipped",
            findings=[],
        )
    models = load_conductor_models()
    model = models.pr_review if task.mission in ("pr_review",) else models.workers
    messages = [
        {"role": "system", "content": system_for(task.mission)},
        {
            "role": "user",
            "content": (
                f"Task id: {task.id}\nTitle: {task.title}\n"
                f"Intensity: {task.intensity}\n\n{task.prompt}"
            ),
        },
    ]
    try:
        # Claude 5.x thinking counts toward max_tokens; 4096 can truncate the JSON reply.
        raw = completion(messages, model=model, max_tokens=16000, note=f"{task.mission}:{task.id}")
        return parse_worker_json(raw, task.id)
    except Exception as exc:
        logger.exception("worker %s failed", task.id)
        return WorkerResult(
            task_id=task.id,
            status="FAILED",
            summary=str(exc),
            escalate_reason=str(exc),
        )


def run_workers_parallel(
    tasks: list[WorkerTask],
    *,
    max_workers: int = 4,
    dry_run: bool = False,
    should_cancel: Callable[[], bool] | None = None,
) -> list[WorkerResult]:
    """Dispatch workers; returns as they complete. Cancel check between completions."""
    if not tasks:
        return []
    results: list[WorkerResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures: dict[Future[WorkerResult], WorkerTask] = {
            pool.submit(run_worker, t, dry_run=dry_run): t for t in tasks
        }
        for fut in as_completed(futures):
            if should_cancel and should_cancel():
                for pending in futures:
                    pending.cancel()
                break
            try:
                results.append(fut.result())
            except Exception as exc:
                t = futures[fut]
                results.append(
                    WorkerResult(task_id=t.id, status="FAILED", summary=str(exc))
                )
    return results


def write_worker_handoff(task: WorkerTask, result: WorkerResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{task.id}.json"
    import json

    path.write_text(
        json.dumps({"task": {"id": task.id, "mission": task.mission, "title": task.title}, "result": result.to_dict()}, indent=2),
        encoding="utf-8",
    )
    return path
