"""FastAPI service exposing the ai-code-auditor as a submit-poll HTTP API.

Endpoints
---------
GET  /audit/health                    VRAM info + service readiness (gothmog capacity check)
POST /audit/run                       Start a new audit run; returns {run_id} immediately
GET  /audit/runs                      List recent runs from PostgreSQL (newest first)
GET  /audit/runs/{run_id}             Poll run status and aggregate results (live metadata mid-run)
GET  /audit/runs/{run_id}/repos       Per-repo rows (status, errors) updated during the run
GET  /audit/runs/{run_id}/stream      SSE: snapshot events until the run leaves pending/running
POST /audit/runs/{run_id}/cancel      Stop an in-flight run (pending/running); idempotent if cancelled
DELETE /audit/runs/{run_id}           Remove a finished run and related DB rows (not while active)
GET  /audit/reports/{run_id}          Return combined markdown report for a completed run
POST /audit/ingest                    Trigger RAG briefing ingest to mimiri/audit_docs

Start:
    .venv/bin/uvicorn auditor.audit_api:app --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.config import load_ecosystem_config
from auditor.db import AuditRun, FindingRecord, PipelineCheckpoint, RepoRun, db_session, utcnow
from auditor.gpu_scheduler import get_vram_info

logger = logging.getLogger(__name__)

app = FastAPI(title="ai-code-auditor API", version="1.0.0")

# ---------------------------------------------------------------------------
# Active-run tracking (in-memory, single slot)
# ---------------------------------------------------------------------------

_active_run_id: str | None = None
_active_process: subprocess.Popen | None = None
_active_run_lock = threading.Lock()
_cancel_event = threading.Event()


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output_dir() -> str:
    try:
        return load_ecosystem_config().output_dir
    except Exception:
        return "reports"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_insert_pending(run_id: str) -> None:
    with db_session() as session:
        session.add(
            AuditRun(
                run_id=run_id,
                pipeline="ecosystem-audit",
                started_at=utcnow(),
                finished_at=None,
                status="pending",
                metadata_json={},
            )
        )


def _db_update_status(run_id: str, status: str) -> None:
    with db_session() as session:
        row = session.execute(
            select(AuditRun).where(AuditRun.run_id == run_id)
        ).scalar_one_or_none()
        if row is not None:
            row.status = status
            if status in ("completed", "failed", "cancelled"):
                row.finished_at = utcnow()


def _db_claim_pending_run(run_id: str) -> bool:
    """Set ``pending`` → ``running``.  False if missing, not pending, or lost the race to cancel."""
    with db_session() as session:
        row = session.execute(
            select(AuditRun).where(AuditRun.run_id == run_id)
        ).scalar_one_or_none()
        if row is None or row.status != "pending":
            return False
        row.status = "running"
        return True


def _db_finish_run_cancelled(run_id: str, note: str = "Run cancelled.") -> None:
    with db_session() as session:
        row = session.execute(
            select(AuditRun).where(AuditRun.run_id == run_id)
        ).scalar_one_or_none()
        if row is None:
            return
        if row.status not in ("pending", "running"):
            return
        row.status = "cancelled"
        row.finished_at = utcnow()
        meta = dict(row.metadata_json or {})
        errs = list(meta.get("errors", []))
        if note and (not errs or errs[-1] != note):
            errs.append(note)
        meta["errors"] = errs
        meta["current_stage"] = "cancelled"
        row.metadata_json = meta


def _db_merge_cancelled_stderr(run_id: str, stderr_tail: str) -> None:
    if not stderr_tail:
        return
    with db_session() as session:
        row = session.execute(
            select(AuditRun).where(AuditRun.run_id == run_id)
        ).scalar_one_or_none()
        if row is None or row.status != "cancelled":
            return
        meta = dict(row.metadata_json or {})
        errs = list(meta.get("errors", []))
        msg = f"Pipeline output after cancel: {stderr_tail[-1500:]}"
        if msg not in errs:
            errs.append(msg)
        meta["errors"] = errs
        row.metadata_json = meta


def _db_delete_run_cascade(run_id: str) -> bool:
    """Delete ``audit_runs`` row and related ``repo_runs`` / ``findings`` / checkpoints."""
    with db_session() as session:
        row = session.execute(
            select(AuditRun).where(AuditRun.run_id == run_id)
        ).scalar_one_or_none()
        if row is None:
            return False
        session.execute(delete(FindingRecord).where(FindingRecord.run_id == run_id))
        session.execute(delete(RepoRun).where(RepoRun.run_id == run_id))
        session.execute(delete(PipelineCheckpoint).where(PipelineCheckpoint.run_id == run_id))
        session.execute(delete(AuditRun).where(AuditRun.run_id == run_id))
    return True


def _db_get_run(run_id: str) -> dict[str, Any] | None:
    with db_session() as session:
        row = session.execute(
            select(AuditRun).where(AuditRun.run_id == run_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return _row_to_dict(row)


def _db_list_runs(limit: int) -> list[dict[str, Any]]:
    with db_session() as session:
        rows = session.execute(
            select(AuditRun)
            .where(AuditRun.pipeline == "ecosystem-audit")
            .order_by(AuditRun.started_at.desc())
            .limit(limit)
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


def _elapsed_ms(row: AuditRun) -> int | None:
    if row.started_at is None:
        return None
    end = row.finished_at if row.finished_at is not None else utcnow()
    return int((end - row.started_at).total_seconds() * 1000)


def _row_to_dict(row: AuditRun) -> dict[str, Any]:
    meta: dict = row.metadata_json or {}
    duration_ms = meta.get("duration_ms")
    if duration_ms is None:
        duration_ms = _elapsed_ms(row)
    repos_total = int(meta.get("repos_total", 0) or 0)
    scanned = meta.get("scanned_repos") or []
    scanned_n = len(scanned) if isinstance(scanned, list) else 0
    return {
        "run_id":        row.run_id,
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


def _db_list_repo_runs(run_id: str) -> list[dict[str, Any]]:
    with db_session() as session:
        rows = session.execute(
            select(RepoRun).where(RepoRun.run_id == run_id).order_by(RepoRun.updated_at.asc())
        ).scalars().all()
        return [
            {
                "repo_name":     r.repo_name,
                "status":        r.status,
                "attempts":      r.attempts,
                "error_message": r.error_message or "",
                "updated_at":    r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Background pipeline worker
# ---------------------------------------------------------------------------

def _run_pipeline(run_id: str) -> None:
    global _active_run_id, _active_process
    pipeline_script = Path(__file__).resolve().parents[1] / "pipelines" / "ecosystem_audit.py"
    env = {**os.environ, "AI_AUDIT_RUN_ID": run_id}
    stderr = ""
    rc: int | None = None

    try:
        if not _db_claim_pending_run(run_id):
            return

        if _cancel_event.is_set():
            # HTTP handler already moved the row to ``cancelled``.
            return

        proc = subprocess.Popen(
            [sys.executable, str(pipeline_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        with _active_run_lock:
            _active_process = proc
        _stdout, stderr = proc.communicate()
        rc = proc.returncode
    except Exception as exc:
        logger.exception("Audit pipeline thread raised: %s", exc)
        if not _cancel_event.is_set():
            _db_update_status(run_id, "failed")
    finally:
        with _active_run_lock:
            _active_process = None
            _active_run_id = None

    if _cancel_event.is_set():
        _db_merge_cancelled_stderr(run_id, stderr)
        return

    if rc is None:
        return

    try:
        if rc == 0:
            _db_update_status(run_id, "completed")
        else:
            stderr_tail = stderr[-2000:] if stderr else ""
            with db_session() as session:
                row = session.execute(
                    select(AuditRun).where(AuditRun.run_id == run_id)
                ).scalar_one_or_none()
                if row is not None:
                    meta = dict(row.metadata_json or {})
                    errs = list(meta.get("errors", []))
                    errs.append(f"Pipeline exit {rc}: {stderr_tail}")
                    meta["errors"] = errs
                    row.metadata_json = meta
                    row.status = "failed"
                    row.finished_at = utcnow()
    except Exception:
        logger.exception("Failed to finalize audit run status for %s", run_id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/audit/health")
def health() -> JSONResponse:
    """VRAM availability and service readiness for gothmog's capacity check."""
    vram = get_vram_info()
    with _active_run_lock:
        active = _active_run_id

    body: dict[str, Any] = {
        "status":        "ok",
        "active_run_id": active,
        "gpu_present":   vram is not None,
        "vram_free_mb":  vram.free_mb  if vram else 0,
        "vram_total_mb": vram.total_mb if vram else 0,
        "vram_used_mb":  vram.used_mb  if vram else 0,
    }
    return JSONResponse(body)


@app.post("/audit/run", status_code=202)
def start_run() -> JSONResponse:
    """Start a new ecosystem audit run.

    Returns ``{run_id}`` immediately.  The run_id is written to PostgreSQL
    as ``status=pending`` before the subprocess starts, so
    ``GET /audit/runs/{run_id}`` is usable immediately.
    Rejects with 409 if a run is already active.
    """
    global _active_run_id
    with _active_run_lock:
        if _active_run_id is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Run {_active_run_id!r} is already active.",
            )
        _cancel_event.clear()
        run_id = f"ecosystem-audit-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        _active_run_id = run_id

    _db_insert_pending(run_id)

    t = threading.Thread(
        target=_run_pipeline,
        args=(run_id,),
        daemon=True,
        name=f"audit-{run_id[-12:]}",
    )
    t.start()

    return JSONResponse({"run_id": run_id})


@app.get("/audit/runs")
def list_runs(limit: int = Query(default=20, ge=1, le=200)) -> JSONResponse:
    """Return the most recent audit runs from PostgreSQL, newest first."""
    runs = _db_list_runs(limit)
    # If there's an active run and it isn't reflected in the DB yet, inject it.
    with _active_run_lock:
        active = _active_run_id
    if active and (not runs or runs[0]["run_id"] != active):
        live = _db_get_run(active)
        if live:
            runs = [r for r in runs if r["run_id"] != active]
            runs.insert(0, live)
    return JSONResponse(runs)


@app.get("/audit/runs/{run_id}/repos")
def list_run_repos(run_id: str) -> JSONResponse:
    """Per-repository status rows, updated as the pipeline processes each repo."""
    if _db_get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")
    return JSONResponse(_db_list_repo_runs(run_id))


@app.get("/audit/runs/{run_id}/stream")
def stream_run(
    run_id: str,
    interval: float = Query(default=1.0, ge=0.25, le=5.0, description="Poll interval in seconds."),
) -> StreamingResponse:
    """Server-Sent Events: repeated ``snapshot`` events with the same JSON as ``GET /audit/runs/{run_id}``.

    Emits ``snapshot`` whenever the payload changes, then ``terminal`` when the run is no longer
    ``pending`` or ``running``, then closes the stream. Sends ``error`` and closes if the run id
    is unknown.
    """

    def event_generator():
        last_payload: str | None = None
        while True:
            row = _db_get_run(run_id)
            if row is None:
                yield f"event: error\ndata: {json.dumps({'detail': f'Run {run_id!r} not found.'})}\n\n"
                return
            payload = json.dumps(row, separators=(",", ":"))
            if payload != last_payload:
                last_payload = payload
                yield f"event: snapshot\ndata: {payload}\n\n"
            if row["status"] not in ("pending", "running"):
                yield f"event: terminal\ndata: {json.dumps({'status': row['status']})}\n\n"
                return
            time.sleep(interval)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/audit/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    """Return status and results for a specific run."""
    row = _db_get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")
    return JSONResponse(row)


@app.post("/audit/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> JSONResponse:
    """Mark an in-flight ecosystem audit as ``cancelled`` and SIGTERM the subprocess when tracked."""
    with db_session() as session:
        row = session.execute(
            select(AuditRun).where(AuditRun.run_id == run_id)
        ).scalar_one_or_none()
    if row is None or row.pipeline != "ecosystem-audit":
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")
    if row.status == "cancelled":
        out = _db_get_run(run_id)
        assert out is not None
        return JSONResponse(out)
    if row.status not in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"Run status is {row.status!r}; only pending or running can be cancelled.",
        )

    _db_finish_run_cancelled(run_id, "Cancelled by operator.")
    proc: subprocess.Popen | None = None
    with _active_run_lock:
        if _active_run_id == run_id:
            _cancel_event.set()
            proc = _active_process
    if proc is not None and proc.poll() is None:
        proc.terminate()

    out = _db_get_run(run_id)
    assert out is not None
    return JSONResponse(out)


@app.delete("/audit/runs/{run_id}", status_code=204)
def delete_run(run_id: str) -> None:
    """Remove a finished ecosystem audit row and related child rows from the database."""
    with db_session() as session:
        row = session.execute(
            select(AuditRun).where(AuditRun.run_id == run_id)
        ).scalar_one_or_none()
    if row is None or row.pipeline != "ecosystem-audit":
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")
    if row.status in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail="Run is still active; cancel it or wait for completion before deleting.",
        )
    if _active_run_id == run_id:
        raise HTTPException(
            status_code=409,
            detail="Run is still bound to the active audit slot; cancel it first.",
        )
    if not _db_delete_run_cascade(run_id):
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")
    return None


@app.get("/audit/reports/{run_id}", response_class=PlainTextResponse)
def get_report(run_id: str) -> str:
    """Return the combined markdown reports for a completed run.

    Reports live in the configured output_dir (default: ``reports/``).
    They reflect the most recent completed run — if the requested run_id
    matches, return them; if it is older, return 410 Gone.
    """
    out = Path(_output_dir())

    # Verify the run exists in the DB.
    row = _db_get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")

    st = row["status"]
    if st in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail="Run is still in progress; the combined markdown report is not available yet.",
        )
    if st in ("failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run finished with status {st!r}; a combined markdown report is not available "
                "for this outcome."
            ),
        )
    if st not in ("completed", "partial_success"):
        raise HTTPException(
            status_code=409,
            detail=f"Run status is {st!r}; report is not available from this endpoint.",
        )

    # Check that on-disk reports belong to this run (the JSON sentinel file).
    sentinel = out / f"run_{run_id}.json"
    if not sentinel.exists():
        raise HTTPException(
            status_code=410,
            detail=(
                f"Reports for {run_id!r} are no longer on disk "
                "(overwritten by a later run). Check S3 archive if configured."
            ),
        )

    md_files = sorted(out.glob("*.md"))
    if not md_files:
        raise HTTPException(status_code=404, detail="No markdown reports found.")

    sections: list[str] = []
    for f in md_files:
        sections.append(f"<!-- {f.name} -->\n\n{f.read_text(encoding='utf-8')}")

    return "\n\n---\n\n".join(sections)


class _ValidateRequest(BaseModel):
    repo: str
    timeout_s: int = 120


@app.post("/audit/validate")
def validate_repo(request: _ValidateRequest) -> JSONResponse:
    """Run the test/lint suite for a configured repository.

    Returns immediately with skipped=true when the repo path is read-only
    (e.g. SSHFS mount) or when no test runner is found.
    """
    from auditor.validator import validate_repo as _validate

    cfg = load_ecosystem_config()
    repo = next((r for r in cfg.repos if r.name == request.repo), None)
    if repo is None:
        raise HTTPException(
            status_code=404,
            detail=f"Repo {request.repo!r} not found in ecosystem config.",
        )

    try:
        result = _validate(repo, timeout_s=max(10, min(request.timeout_s, 600)))
    except Exception as exc:
        logger.exception("validate failed for repo %r", request.repo)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({
        "repo": request.repo,
        "passed": result.passed,
        "tool": result.tool,
        "duration_ms": result.duration_ms,
        "errors": result.errors,
        "raw_output": result.raw_output[-4000:],
        "skipped_reason": result.skipped_reason,
    })


class _DiffAuditRequest(BaseModel):
    repo: str
    diff: str = ""
    compare_branch: str = ""


@app.post("/audit/diff")
def audit_diff(request: _DiffAuditRequest) -> JSONResponse:
    """Audit the files touched by a unified diff.

    Supply either ``diff`` (raw unified diff text) or ``compare_branch``
    (generates ``git diff <branch>...HEAD`` from the configured repo path).
    """
    from auditor.diff_auditor import get_diff_from_branch, run_diff_audit
    from auditor.repo_resolver import resolve_repo_path

    cfg = load_ecosystem_config()
    repo = next((r for r in cfg.repos if r.name == request.repo), None)
    if repo is None:
        raise HTTPException(
            status_code=404,
            detail=f"Repo {request.repo!r} not found in ecosystem config.",
        )

    if request.compare_branch:
        try:
            repo_path = resolve_repo_path(repo)
            diff_text = get_diff_from_branch(repo_path, request.compare_branch)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif request.diff:
        diff_text = request.diff
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide 'diff' (unified diff text) or 'compare_branch'.",
        )

    if not diff_text.strip():
        return JSONResponse({
            "repo": request.repo,
            "files_changed": 0,
            "files_affected": 0,
            "units_analyzed": 0,
            "findings": [],
            "message": "Diff is empty — nothing to audit.",
        })

    try:
        result = run_diff_audit(repo, diff_text)
    except Exception as exc:
        logger.exception("diff-audit failed for repo %r", request.repo)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({
        "repo": result.repo,
        "files_changed": len(result.impact.directly_changed),
        "files_affected": len(result.impact.transitively_affected),
        "units_analyzed": len(result.units_analyzed),
        "findings": [asdict(f) for f in result.findings],
        "generated_at": result.generated_at,
    })


@app.post("/audit/ingest")
def trigger_ingest() -> JSONResponse:
    """Push the latest audit briefings to the mimiri RAG collection (audit_docs)."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "ingest_ecosystem_to_rag.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="ingest_ecosystem_to_rag.py not found.")

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Ingest failed (exit {result.returncode}): {result.stderr[-2000:]}",
        )
    return JSONResponse({"status": "ok", "output": result.stdout[-2000:]})
