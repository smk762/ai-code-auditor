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
from auditor.db import AuditRun, FindingRecord, PipelineCheckpoint, RepoRun, as_utc, db_session, utcnow
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
    started = as_utc(row.started_at)
    if started is None:
        return None
    end = as_utc(row.finished_at) or utcnow()
    return int((end - started).total_seconds() * 1000)


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


class _ValidatePatchRequest(BaseModel):
    repo: str
    patch: str
    timeout_s: int = 120


@app.post("/audit/validate_patch")
def validate_patch_endpoint(request: _ValidatePatchRequest) -> JSONResponse:
    """Apply *patch* to a temp copy of *repo* and run its test/lint suite.

    This is the primary feedback mechanism for the repair loop with local models:
    instead of returning thousands of lines of raw test output, the response
    includes a ``failure_summary`` — a compact list of actionable failure lines
    that fits comfortably in a local model's context window.

    Returns:
        patch_applied:   bool — whether ``git apply`` succeeded.
        patch_error:     str  — git apply stderr when patch_applied is False.
        passed:          bool — True only when patch applied AND suite passed.
        tool:            str  — runner used (pytest / go_test / cargo_test / …).
        duration_ms:     int
        errors:          list[str] — raw error lines from the runner.
        failure_summary: list[str] — compact LLM-ready failure lines (≤20).
        raw_output:      str  — last 2000 chars of runner output.
        skipped_reason:  str  — non-empty when validation was skipped.
    """
    import shutil
    import tempfile

    from auditor.validator import extract_failure_summary
    from auditor.validator import validate_repo as _validate
    from auditor.repo_resolver import resolve_repo_path

    cfg = load_ecosystem_config()
    repo_cfg = next((r for r in cfg.repos if r.name == request.repo), None)
    if repo_cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Repo {request.repo!r} not found in ecosystem config.",
        )

    try:
        repo_path = resolve_repo_path(repo_cfg)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot resolve repo path: {exc}") from exc

    timeout_s = max(10, min(request.timeout_s, 600))
    tmp_root = Path(tempfile.mkdtemp(prefix="ai-validate-patch-"))
    patch_applied = False
    patch_error = ""

    try:
        repo_copy = tmp_root / "repo"
        shutil.copytree(str(repo_path), str(repo_copy), symlinks=True)

        patch_file = tmp_root / "changes.patch"
        patch_file.write_text(request.patch, encoding="utf-8")

        apply_result = subprocess.run(
            ["git", "apply", "--whitespace=fix", str(patch_file)],
            cwd=repo_copy,
            capture_output=True,
            text=True,
        )
        if apply_result.returncode != 0:
            patch_error = apply_result.stderr.strip()[:600]
        else:
            patch_applied = True

        val_result = _validate(
            repo_cfg,
            path_override=repo_copy if patch_applied else None,
            timeout_s=timeout_s,
        )

        summary = extract_failure_summary(val_result.raw_output, val_result.tool)
        passed = patch_applied and val_result.passed

        # Infrastructure errors: runner failed but produced no actionable failure lines.
        # Log as warning so container logs surface the problem (broken venv, missing
        # binary, bad shebang after copytree from SSHFS, etc.).
        if not passed and not summary and val_result.errors and not val_result.skipped_reason:
            logger.warning(
                "validate_patch: runner %r reported failure with no actionable output "
                "(repo=%s, patch_applied=%s). Infra errors: %s",
                val_result.tool,
                request.repo,
                patch_applied,
                val_result.errors[:3],
            )

        return JSONResponse({
            "repo":            request.repo,
            "patch_applied":   patch_applied,
            "patch_error":     patch_error,
            "passed":          passed,
            "tool":            val_result.tool,
            "duration_ms":     val_result.duration_ms,
            "errors":          val_result.errors,
            "failure_summary": summary,
            "raw_output":      val_result.raw_output[-2000:],
            "skipped_reason":  val_result.skipped_reason,
        })
    except Exception as exc:
        logger.exception("validate_patch failed for repo %r", request.repo)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


# ── Repo status + raw diff endpoints ─────────────────────────────────────────

@app.get("/audit/repos/status")
def repos_status() -> JSONResponse:
    """Return live status for every enabled repo.

    For each repo: current branch, clean/dirty state, and a diff stat against
    the configured base branch (or main/master if none configured).  Useful
    for deciding which repos have changes worth feeding into the repair loop.

    The diff stat is obtained via ``git diff --shortstat <base>...HEAD`` which
    is fast (no LLM calls).  Repos where the base branch ref is unknown (e.g.
    the remote branch hasn't been fetched) report ``diff_error`` instead of
    crashing the whole response.
    """
    from auditor.git_ops import get_current_branch, is_working_tree_clean, is_in_conflict_state
    from auditor.repo_resolver import resolve_repo_path, select_branch

    cfg = load_ecosystem_config()
    results = []

    for repo in cfg.repos:
        if not repo.enabled:
            continue

        entry: dict = {
            "name":         repo.name,
            "branch":       "",
            "base_branch":  "",
            "is_clean":     None,
            "in_conflict":  False,
            "has_diff":     False,
            "files_changed": 0,
            "insertions":   0,
            "deletions":    0,
            "diff_error":   "",
            "path_missing": False,
        }

        try:
            repo_path = resolve_repo_path(repo)
        except Exception as exc:
            entry["diff_error"] = f"path error: {exc}"
            entry["path_missing"] = True
            results.append(entry)
            continue

        entry["branch"]    = get_current_branch(repo_path)
        entry["is_clean"]  = is_working_tree_clean(repo_path)
        entry["in_conflict"] = is_in_conflict_state(repo_path)

        base = select_branch(repo, repo_path)
        entry["base_branch"] = base

        try:
            r = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "--shortstat", f"{base}...HEAD"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                stderr = r.stderr.strip()
                if "unknown revision" in stderr or "bad revision" in stderr:
                    entry["diff_error"] = f"base branch {base!r} not found in local refs"
                else:
                    entry["diff_error"] = stderr[:200]
            else:
                stat = r.stdout.strip()
                entry["has_diff"] = bool(stat)
                if stat:
                    # "3 files changed, 42 insertions(+), 10 deletions(-)"
                    import re
                    fc = re.search(r"(\d+) files? changed", stat)
                    ins = re.search(r"(\d+) insertion", stat)
                    dels = re.search(r"(\d+) deletion", stat)
                    entry["files_changed"] = int(fc.group(1)) if fc else 0
                    entry["insertions"]    = int(ins.group(1)) if ins else 0
                    entry["deletions"]     = int(dels.group(1)) if dels else 0
        except subprocess.TimeoutExpired:
            entry["diff_error"] = "git diff timed out"
        except Exception as exc:
            entry["diff_error"] = str(exc)[:200]

        results.append(entry)

    return JSONResponse(results)


class _RawDiffRequest(BaseModel):
    repo: str
    compare_branch: str


@app.post("/audit/git/diff")
def git_raw_diff(request: _RawDiffRequest) -> JSONResponse:
    """Return the raw unified diff for *repo* between *compare_branch* and HEAD.

    This is the canonical way for external services (e.g. rag-chat repair router)
    to obtain a diff — they should not try to read the ecosystem config or run
    git themselves.

    Returns:
        diff:        str  — raw unified diff text (empty string when no changes).
        is_empty:    bool — True when the diff is empty (branches are identical).
        base_branch: str  — the branch diffed against.
        error:       str  — non-empty when the diff could not be generated.
        reason:      str  — machine-readable reason code for the error.
                           One of: "repo_not_found", "branch_not_found",
                           "path_error", "git_error", "timeout".
    """
    from auditor.repo_resolver import resolve_repo_path

    cfg = load_ecosystem_config()
    repo_cfg = next((r for r in cfg.repos if r.name == request.repo), None)
    if repo_cfg is None:
        return JSONResponse({
            "diff": "", "is_empty": True,
            "base_branch": request.compare_branch,
            "error": f"Repo {request.repo!r} not found in ecosystem config.",
            "reason": "repo_not_found",
        }, status_code=404)

    try:
        repo_path = resolve_repo_path(repo_cfg)
    except Exception as exc:
        return JSONResponse({
            "diff": "", "is_empty": True,
            "base_branch": request.compare_branch,
            "error": f"Cannot resolve repo path: {exc}",
            "reason": "path_error",
        }, status_code=400)

    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "diff",
             f"{request.compare_branch}...HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return JSONResponse({
            "diff": "", "is_empty": True,
            "base_branch": request.compare_branch,
            "error": "git diff timed out after 30s",
            "reason": "timeout",
        })

    if r.returncode != 0:
        stderr = r.stderr.strip()
        if "unknown revision" in stderr or "bad revision" in stderr:
            reason = "branch_not_found"
            error  = (
                f"Branch {request.compare_branch!r} is not a known ref in this repo. "
                f"Available local branches can be found via GET /audit/repos/status."
            )
        else:
            reason = "git_error"
            error  = stderr[:400]
        return JSONResponse({
            "diff": "", "is_empty": True,
            "base_branch": request.compare_branch,
            "error": error, "reason": reason,
        })

    diff_text = r.stdout
    return JSONResponse({
        "diff":        diff_text,
        "is_empty":    not diff_text.strip(),
        "base_branch": request.compare_branch,
        "error":       "",
        "reason":      "",
    })


# ── Git automation endpoints ──────────────────────────────────────────────────
# These are called by the rag-chat repair router after a successful patch apply.
# All operations are local-only (no network calls except push_repair_branch).
# Per-repo locking is not implemented here — the single-slot repair model in
# rag-chat ensures at most one repair runs per repo at a time.

class _GitBranchRequest(BaseModel):
    repo: str
    slug: str
    base_branch: str = ""


class _GitCommitRequest(BaseModel):
    repo: str
    message: str
    patch: str = ""          # if provided, used to determine which files to stage
    author_name: str = "ai-code-auditor"
    author_email: str = "noreply@ai-audit.local"


class _GitPushRequest(BaseModel):
    repo: str
    branch: str = ""         # empty → current branch
    remote: str = "origin"


@app.post("/audit/git/branch")
def git_create_branch(request: _GitBranchRequest) -> JSONResponse:
    """Create (and check out) a ``repair/<slug>`` branch for *repo*.

    Returns:
        branch:      str  — full branch name, e.g. "repair/ext-retry-001".
        base_branch: str  — branch forked from.
        is_new:      bool — False if the branch already existed.
    """
    from auditor.git_ops import create_repair_branch, GitError
    from auditor.repo_resolver import resolve_repo_path

    cfg = load_ecosystem_config()
    repo_cfg = next((r for r in cfg.repos if r.name == request.repo), None)
    if repo_cfg is None:
        raise HTTPException(status_code=404, detail=f"Repo {request.repo!r} not found.")

    try:
        repo_path = resolve_repo_path(repo_cfg)
        result = create_repair_branch(
            repo_path,
            slug=request.slug,
            base_branch=request.base_branch or None,
        )
    except GitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("git_create_branch failed for %r", request.repo)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({
        "repo":        request.repo,
        "branch":      result.branch,
        "base_branch": result.base_branch,
        "is_new":      result.is_new,
    })


@app.post("/audit/git/commit")
def git_commit(request: _GitCommitRequest) -> JSONResponse:
    """Stage the repair-touched files and commit them to the current branch.

    Returns:
        sha:          str  — 40-char commit hash, or "" when nothing was staged.
        branch:       str  — branch committed to.
        files_staged: int  — number of files in the commit.
        skipped:      bool — True when nothing was staged (clean working tree).
    """
    from auditor.git_ops import commit_repair, GitError
    from auditor.repo_resolver import resolve_repo_path

    cfg = load_ecosystem_config()
    repo_cfg = next((r for r in cfg.repos if r.name == request.repo), None)
    if repo_cfg is None:
        raise HTTPException(status_code=404, detail=f"Repo {request.repo!r} not found.")

    try:
        repo_path = resolve_repo_path(repo_cfg)
        result = commit_repair(
            repo_path,
            message=request.message,
            patch_text=request.patch,
            author_name=request.author_name,
            author_email=request.author_email,
        )
    except GitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("git_commit failed for %r", request.repo)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({
        "repo":          request.repo,
        "sha":           result.sha,
        "branch":        result.branch,
        "files_staged":  result.files_staged,
        "short_message": result.short_message,
        "skipped":       result.sha == "",
    })


@app.post("/audit/git/push")
def git_push(request: _GitPushRequest) -> JSONResponse:
    """Push *branch* (default: current branch) to *remote*.

    Never force-pushes.  Returns ``pushed: false`` with a ``skipped_reason``
    when the remote is unconfigured or is a local path — not an HTTP error.

    Returns:
        pushed:         bool
        branch:         str
        remote:         str
        remote_url:     str
        skipped_reason: str — non-empty when pushed=False
    """
    from auditor.git_ops import push_repair_branch, GitError
    from auditor.repo_resolver import resolve_repo_path

    cfg = load_ecosystem_config()
    repo_cfg = next((r for r in cfg.repos if r.name == request.repo), None)
    if repo_cfg is None:
        raise HTTPException(status_code=404, detail=f"Repo {request.repo!r} not found.")

    try:
        repo_path = resolve_repo_path(repo_cfg)
        result = push_repair_branch(
            repo_path,
            branch=request.branch or None,
            remote=request.remote,
        )
    except GitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("git_push failed for %r", request.repo)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({
        "repo":           request.repo,
        "pushed":         result.pushed,
        "branch":         result.branch,
        "remote":         result.remote,
        "remote_url":     result.remote_url,
        "skipped_reason": result.skipped_reason,
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
