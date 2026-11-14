"""JSON shapes for audit runs, shared by the HTTP API and the web UI.

Run status vocabulary (``audit_runs.status``):

* ``pending`` / ``running``: in flight.
* ``success`` / ``partial_success``: finished, written by the pipeline (timer or CLI runs).
* ``completed``: finished; ``POST /audit/run`` overwrites the pipeline's status with this
  after a zero exit code.
* ``failed`` / ``cancelled``.
"""
from __future__ import annotations

from typing import Any

from auditor.db import AuditRun, RepoRun, as_utc, utcnow

ACTIVE_STATUSES = ("pending", "running")
SUCCESS_STATUSES = ("success", "completed", "partial_success")


def elapsed_ms(row: AuditRun) -> int | None:
    started = as_utc(row.started_at)
    if started is None:
        return None
    end = as_utc(row.finished_at) or utcnow()
    return int((end - started).total_seconds() * 1000)


def run_to_dict(row: AuditRun) -> dict[str, Any]:
    meta: dict = row.metadata_json or {}
    duration_ms = meta.get("duration_ms")
    if duration_ms is None:
        duration_ms = elapsed_ms(row)
    repos_total = int(meta.get("repos_total", 0) or 0)
    scanned = meta.get("scanned_repos") or []
    scanned_n = len(scanned) if isinstance(scanned, list) else 0
    return {
        "run_id":        row.run_id,
        "pipeline":      row.pipeline,
        "status":        row.status,
        "started_at":    row.started_at.isoformat() if row.started_at else None,
        "finished_at":   row.finished_at.isoformat() if row.finished_at else None,
        "scanned_repos": scanned if isinstance(scanned, list) else [],
        "scanned_files": int(meta.get("scanned_files", 0) or 0),
        "code_units":    int(meta.get("code_units", 0) or 0),
        "findings":      int(meta.get("findings", 0) or 0),
        "violations":    int(meta.get("violations", 0) or 0),
        "errors":        meta.get("errors", []),
        "duration_ms":   duration_ms,
        "current_stage": meta.get("current_stage") or "",
        "repos_total":   repos_total,
        "repos_completed": scanned_n,
        "stage_timings_ms": meta.get("stage_timings_ms", {}),
        "stage_status":     meta.get("stage_status", {}),
        "extra":         meta.get("extra", {}),
    }


def repo_run_to_dict(r: RepoRun) -> dict[str, Any]:
    return {
        "repo_name":     r.repo_name,
        "status":        r.status,
        "attempts":      r.attempts,
        "error_message": r.error_message or "",
        "updated_at":    r.updated_at.isoformat() if r.updated_at else None,
    }
