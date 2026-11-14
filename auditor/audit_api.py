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
GET  /audit/topology                  Return the latest service topology HTML
GET  /audit/topology.json             Return the latest service topology JSON sidecar
GET  /                               Web UI landing page (also /ui); shared nav + /audit/static assets
GET  /audit/summary.json             Landing-page headline numbers (last run, findings, packs, spend)
GET  /audit/usage                    Claude API usage dashboard (+ /audit/usage.json)
GET  /reports/mithrandir/LATEST.json  Atomic rest-session status for mithrandir (Jarrah)
GET  /reports/mithrandir/LATEST.md    TTS-safe companion briefing for Holly
POST /audit/ingest                    Trigger RAG briefing ingest to the audit_docs collection
POST /audit/merge                     Schedule lightweight merge-to-dev pack/RAG refresh (debounced)
GET  /audit/merge                     Pending/running merge-hook status
POST /audit/hooks/github              GitHub push webhook (dev branch → merge refresh)
GET  /audit/packs                     List/search agent coding packs (repo, severity, kind, layer, status, q)
GET  /audit/packs/bundle              Mission bundle: world model + ranked specialists, token-capped briefs
GET  /audit/packs/{pack_id}           Full agent coding pack (YAML-backed JSON)
GET  /audit/packs/{pack_id}/brief     Markdown brief for coding agents
POST /audit/packs/{pack_id}/status    Set pack lifecycle status (open|claimed|in_progress|resolved|wontfix)

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
from typing import Any, Literal

from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.agent_pack import CHARS_PER_TOKEN, DEFAULT_BUNDLE_TOKEN_BUDGET, PACK_DISCOVERY
from auditor.config import load_ecosystem_config
from auditor.db import AuditRun, FindingRecord, PipelineCheckpoint, RepoRun, as_utc, db_session, utcnow
from auditor.gpu_scheduler import get_vram_info
from auditor.ui import STATIC_DIR, STATIC_URL, render_page

logger = logging.getLogger(__name__)

app = FastAPI(title="ai-code-auditor API", version="1.0.0")
app.mount(STATIC_URL, StaticFiles(directory=STATIC_DIR), name="static")


def _require_mutating_auth(
    authorization: str | None = Header(default=None),
    x_auth_code: str | None = Header(default=None, alias="X-Auth-Code"),
    x_api_token: str | None = Header(default=None, alias="X-API-Token"),
) -> None:
    """Gate write endpoints (git mutate, validate_patch, run, ingest, cancel).

    Auth is required when ``AI_AUDIT_AUTH_MODE`` is enabled or ``AI_AUDIT_API_TOKEN``
    is set. Accepts Bearer JWT (admin), ``X-Auth-Code``, or ``X-API-Token``.
    When neither mode is configured (local/dev), requests are allowed.
    """
    cfg = load_ecosystem_config()
    api_token = os.getenv("AI_AUDIT_API_TOKEN", "").strip()
    if not cfg.auth_mode and not api_token:
        return

    if api_token and (x_api_token or "").strip() == api_token:
        return

    if cfg.auth_mode:
        expected_env = cfg.auth_code_env or "AI_AUDIT_AUTH_CODE"
        expected = os.getenv(expected_env, "")
        if expected and (x_auth_code or "") == expected:
            return

    bearer = (authorization or "").strip()
    if bearer.lower().startswith("bearer "):
        token = bearer[7:].strip()
        if token:
            try:
                from auditor.auth import enforce_role

                enforce_role(token, "admin", "api:mutating")
                return
            except PermissionError as exc:
                raise HTTPException(status_code=401, detail="Invalid or unauthorized JWT") from exc

    raise HTTPException(
        status_code=401,
        detail="Mutating endpoint requires X-API-Token, X-Auth-Code, or Bearer admin JWT",
    )


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


@app.post("/audit/run", status_code=202, dependencies=[Depends(_require_mutating_auth)])
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
            else:
                # Comment frame keeps proxies (nginx, docker networks) from
                # closing the idle stream during long LLM calls.
                yield ": keepalive\n\n"
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


@app.post("/audit/runs/{run_id}/cancel", dependencies=[Depends(_require_mutating_auth)])
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


@app.delete("/audit/runs/{run_id}", status_code=204, dependencies=[Depends(_require_mutating_auth)])
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


@app.get("/audit/topology")
def get_service_topology() -> FileResponse:
    """Return the latest service topology HTML.  404 if not yet generated."""
    path = Path(_output_dir()) / "service_topology.html"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Service topology has not been generated yet — run an ecosystem audit first.",
        )
    return FileResponse(path, media_type="text/html")


@app.get("/audit/usage", response_class=HTMLResponse)
def get_usage_dashboard() -> HTMLResponse:
    """Claude API usage dashboard (spend by lane, model, process, day and hour)."""
    from auditor.usage_dashboard import HTML_PATH

    return HTMLResponse(render_page(HTML_PATH.name, "/audit/usage"))


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def get_landing_page() -> HTMLResponse:
    """Web UI landing page: last run, findings by severity, open packs, month spend."""
    return HTMLResponse(render_page("index.html", "/"))


@app.get("/audit/summary.json")
def get_summary_json() -> JSONResponse:
    """Headline numbers for the landing page; sections that fail to load are listed in ``errors``."""
    from auditor.ui import summary_data

    return JSONResponse(summary_data(_output_dir()))


@app.get("/audit/usage.json")
def get_usage_json(range: str = "month") -> JSONResponse:
    """Spend-ledger aggregates for the usage dashboard. range: month | 7d | 14d | 30d."""
    from auditor.usage_dashboard import usage_data

    if range not in {"month", "7d", "14d", "30d", "90d"}:
        raise HTTPException(status_code=400, detail="range must be month, 7d, 14d, 30d or 90d")
    return JSONResponse(usage_data(range))


@app.get("/audit/prs")
def get_pr_dashboard() -> FileResponse:
    """Blocked-PR dashboard: what is stuck, why, and the suggested order to clear it."""
    from auditor.pr_intel import HTML_PATH

    return FileResponse(HTML_PATH, media_type="text/html")


@app.get("/audit/prs.json")
def get_pr_intel_json() -> JSONResponse:
    """Cached PR intel (refreshed by the hourly merge poll). 404 until the first refresh."""
    from auditor.pr_intel import load_report

    report = load_report(_output_dir())
    if report is None:
        raise HTTPException(
            status_code=404,
            detail="No PR intel yet: run `ai-audit pr-intel` or POST /audit/prs/refresh.",
        )
    return JSONResponse(report)


@app.post("/audit/prs/refresh", dependencies=[Depends(_require_mutating_auth)])
def post_pr_intel_refresh() -> JSONResponse:
    """Rebuild PR intel from GitHub now (read-only on GitHub; rewrites the cache)."""
    from auditor.pr_intel import refresh

    try:
        report = refresh(output_dir=_output_dir())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(report)


@app.get("/audit/topology.json")
def get_service_topology_json() -> FileResponse:
    """Return the structured JSON sidecar for the latest topology.  404 if absent."""
    path = Path(_output_dir()) / "service_topology.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Service topology JSON has not been generated yet.",
        )
    return FileResponse(path, media_type="application/json")


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
        "skipped": bool(result.skipped_reason),
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


class _MergeRefreshRequest(BaseModel):
    repo: str
    before: str = ""
    after: str = ""
    ref: str = ""
    force: bool = False  # skip debounce (still serialised behind any running job)
    sync_mirror: bool = True


@app.get("/audit/merge")
def merge_hook_status() -> JSONResponse:
    """Pending / running / recent lightweight merge-to-dev refreshes."""
    from auditor.merge_hook import get_scheduler

    return JSONResponse(get_scheduler().status())


@app.post("/audit/merge", status_code=202, dependencies=[Depends(_require_mutating_auth)])
def schedule_merge_refresh(request: _MergeRefreshRequest) -> JSONResponse:
    """Queue a single-repo mirror sync + diff pack/RAG refresh after a merge to ``dev``.

    Close pushes to the same repo are coalesced for ``AI_AUDIT_MERGE_DEBOUNCE_S``
    (default 900s). Distinct repos wait ``AI_AUDIT_MERGE_GLOBAL_GAP_S`` (default 120s)
    between runs. Pass ``force=true`` to skip the per-repo quiet window.
    """
    from auditor.merge_hook import MergeEvent, get_scheduler, merge_hook_enabled

    if not merge_hook_enabled() and not request.force:
        raise HTTPException(status_code=503, detail="Merge hook disabled (AI_AUDIT_MERGE_HOOK_ENABLED=0)")

    event = MergeEvent(
        repo=request.repo.strip(),
        before=request.before,
        after=request.after,
        ref=request.ref,
        source="api",
        force=bool(request.force),
        sync_mirror=bool(request.sync_mirror),
    )
    result = get_scheduler().enqueue(event)
    if not result.get("accepted"):
        raise HTTPException(status_code=400, detail=result.get("reason") or "rejected")
    return JSONResponse(result, status_code=202)


@app.post("/audit/hooks/github")
async def github_merge_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
) -> JSONResponse:
    """GitHub push webhook → debounced merge refresh when ``refs/heads/<branch>``.

    Auth: ``AI_AUDIT_MERGE_WEBHOOK_SECRET`` via ``X-Hub-Signature-256``. When the
    secret is unset, falls back to the normal mutating API auth (or open if none).
    """
    from auditor.merge_hook import (
        get_scheduler,
        merge_hook_enabled,
        merge_webhook_secret,
        parse_github_push_event,
        verify_github_signature,
    )

    body = await request.body()
    secret = merge_webhook_secret()
    if secret:
        if not verify_github_signature(body, x_hub_signature_256, secret):
            raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
    else:
        # No dedicated webhook secret — require mutating auth headers if configured.
        _require_mutating_auth(
            authorization=request.headers.get("authorization"),
            x_auth_code=request.headers.get("x-auth-code"),
            x_api_token=request.headers.get("x-api-token"),
        )

    if not merge_hook_enabled():
        return JSONResponse({"accepted": False, "reason": "merge hook disabled"})

    event_name = (x_github_event or "").strip().lower()
    if event_name and event_name != "push":
        return JSONResponse({"accepted": False, "reason": f"ignored event {event_name}"})

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")

    event = parse_github_push_event(payload)
    if event is None:
        return JSONResponse(
            {
                "accepted": False,
                "reason": "ignored (not push to configured merge branch, or missing repo)",
            }
        )

    result = get_scheduler().enqueue(event)
    return JSONResponse(result, status_code=202 if result.get("accepted") else 200)


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

    from auditor.agent_pack import emit_packs_from_findings
    from conductor.publish import public_base_url

    packs = []
    try:
        packs = emit_packs_from_findings(
            result.findings,
            output_dir=_output_dir(),
            repo_path=resolve_repo_path(repo),
            kind="diff_audit",
            run_id=f"diff-{request.repo}",
            directly_changed=sorted(result.impact.directly_changed),
            transitively_affected=sorted(result.impact.transitively_affected),
            public_base_url=public_base_url(),
            ingest_rag=False,
        )
    except Exception:
        logger.exception("failed to emit agent packs for diff-audit %r", request.repo)

    return JSONResponse({
        "repo": result.repo,
        "files_changed": len(result.impact.directly_changed),
        "files_affected": len(result.impact.transitively_affected),
        "directly_changed": sorted(result.impact.directly_changed),
        "transitively_affected": sorted(result.impact.transitively_affected)[:100],
        "units_analyzed": len(result.units_analyzed),
        "findings": [asdict(f) for f in result.findings],
        "agent_packs": [p.work_item_id for p in packs],
        "packs_url": f"{public_base_url()}/audit/packs?repo={request.repo}",
        "generated_at": result.generated_at,
    })


class _ValidatePatchRequest(BaseModel):
    repo: str
    patch: str
    timeout_s: int = 120
    pack_id: str | None = None  # when set and validation passes, the pack is marked resolved


def _resolve_pack_after_validation(
    pack_id: str, *, repo: str, tool: str
) -> dict[str, Any]:
    """Mark *pack_id* resolved after a passing validate_patch. Never raises."""
    from auditor.agent_pack import load_pack, update_pack_status
    from conductor.publish import public_base_url

    out = _output_dir()
    try:
        data = load_pack(out, pack_id)
        if data is None:
            return {"id": pack_id, "updated": False, "reason": "pack not found"}
        pack_repo = str((data.get("repo") or {}).get("name") or "")
        impacted = list((data.get("impact") or {}).get("impacted_repos") or [])
        if data.get("kind") == "world_model" or repo not in {pack_repo, *impacted}:
            return {"id": pack_id, "updated": False, "reason": "pack does not apply to this repo"}
        updated = update_pack_status(
            out,
            pack_id,
            "resolved",
            f"validate_patch passed (repo={repo}, tool={tool})",
            public_base_url=public_base_url(),
        )
        return {"id": pack_id, "updated": updated is not None, "status": "resolved"}
    except Exception as exc:  # closure is best-effort; the validation result stands
        logger.warning("could not mark pack %s resolved: %s", pack_id, exc)
        return {"id": pack_id, "updated": False, "reason": str(exc)}


@app.post("/audit/validate_patch", dependencies=[Depends(_require_mutating_auth)])
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
        # Do not preserve symlinks: a crafted patch must not write through a
        # repo symlink into the host filesystem outside this temp tree.
        shutil.copytree(str(repo_path), str(repo_copy), symlinks=False)

        patch_file = tmp_root / "changes.patch"
        patch_file.write_text(request.patch, encoding="utf-8")

        apply_result = subprocess.run(
            ["git", "apply", "--whitespace=fix", str(patch_file)],
            cwd=repo_copy,
            capture_output=True,
            text=True,
        )
        if apply_result.returncode != 0:
            # Don't run the validator against the unmodified repo — the resulting
            # raw_output / failure_summary would describe code unrelated to the
            # patch under review.
            return JSONResponse({
                "repo":            request.repo,
                "patch_applied":   False,
                "patch_error":     apply_result.stderr.strip()[:600],
                "passed":          False,
                "skipped":         True,
                "tool":            "skipped",
                "duration_ms":     0,
                "errors":          [],
                "failure_summary": [],
                "raw_output":      "",
                "skipped_reason":  "patch did not apply",
            })
        patch_applied = True

        val_result = _validate(
            repo_cfg,
            path_override=repo_copy,
            timeout_s=timeout_s,
        )

        summary = extract_failure_summary(val_result.raw_output, val_result.tool)
        skipped = bool(val_result.skipped_reason)
        passed = val_result.passed and not skipped

        # Infrastructure errors: runner failed but produced no actionable failure lines.
        # Log as warning so container logs surface the problem (broken venv, missing
        # binary, bad shebang after copytree from SSHFS, etc.).
        if not passed and not summary and val_result.errors and not skipped:
            logger.warning(
                "validate_patch: runner %r reported failure with no actionable output "
                "(repo=%s). Infra errors: %s",
                val_result.tool,
                request.repo,
                val_result.errors[:3],
            )

        response: dict[str, Any] = {
            "repo":            request.repo,
            "patch_applied":   patch_applied,
            "patch_error":     patch_error,
            "passed":          passed,
            "skipped":         skipped,
            "tool":            val_result.tool,
            "duration_ms":     val_result.duration_ms,
            "errors":          val_result.errors,
            "failure_summary": summary,
            "raw_output":      val_result.raw_output[-2000:],
            "skipped_reason":  val_result.skipped_reason,
        }
        if request.pack_id:
            response["pack"] = (
                _resolve_pack_after_validation(
                    request.pack_id, repo=request.repo, tool=val_result.tool
                )
                if passed
                else {"id": request.pack_id, "updated": False, "reason": "validation did not pass"}
            )
        return JSONResponse(response)
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


@app.post("/audit/git/branch", dependencies=[Depends(_require_mutating_auth)])
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


@app.post("/audit/git/commit", dependencies=[Depends(_require_mutating_auth)])
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


@app.post("/audit/git/push", dependencies=[Depends(_require_mutating_auth)])
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


@app.get("/reports/mithrandir/LATEST.json")
def mithrandir_latest_json() -> JSONResponse:
    """Serve the latest rest-session status for mithrandir (atomic on-disk publish)."""
    from conductor.publish import mithrandir_dir

    path = mithrandir_dir(_output_dir()) / "LATEST.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="LATEST.json not published yet.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"LATEST.json corrupt: {exc}") from exc
    return JSONResponse(data)


@app.get("/reports/mithrandir/LATEST.md", response_class=PlainTextResponse)
def mithrandir_latest_md() -> str:
    """Serve the TTS-safe companion briefing for Holly."""
    from conductor.publish import mithrandir_dir

    path = mithrandir_dir(_output_dir()) / "LATEST.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="LATEST.md not published yet.")
    return path.read_text(encoding="utf-8")


@app.get("/audit/packs")
def list_agent_packs(
    repo: str = Query(default=""),
    severity: str = Query(default=""),
    kind: str = Query(default="", description="world_model | architecture | extraction | …"),
    layer: str = Query(default="", description="world_model | specialist"),
    q: str = Query(default="", description="Substring match on id/title/repo/path"),
    run_id: str = Query(default=""),
    status: str = Query(
        default="",
        description="open | claimed | in_progress | resolved | wontfix (comma-separated allowed)",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    """Discover agent packs: world model first, then specialist coding packs.

    Coding agents should load ``kind=world_model`` (or ``layer=world_model``) for
    the target repo, then fetch specialist packs — no pasted brief required.
    Prefer ``GET /audit/packs/bundle?repo=…`` for a single-call mission bundle.
    """
    from auditor.agent_pack import index_path, list_packs, rebuild_index
    from conductor.publish import public_base_url

    out = _output_dir()
    if not index_path(out).exists():
        rebuild_index(out, public_base_url=public_base_url())
    packs = list_packs(
        out,
        repo=repo,
        severity=severity,
        kind=kind,
        layer=layer,
        q=q,
        run_id=run_id,
        status=status,
        limit=limit,
    )
    idx_meta: dict[str, Any] = {}
    try:
        idx_meta = json.loads(index_path(out).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return JSONResponse({
        "count": len(packs),
        "loop": idx_meta.get("loop", "scan → compressed world model → specialist coding packs"),
        "layers": idx_meta.get("layers", {}),
        "kind_counts": idx_meta.get("kind_counts", {}),
        "status_counts": idx_meta.get("status_counts", {}),
        "packs": packs,
        # Always serve current discovery (older INDEX.json files point specialist at kind=architecture).
        "discovery": {**PACK_DISCOVERY, "index_file": f"{out}/agent_packs/INDEX.json"},
    })


@app.get("/audit/packs/bundle")
def get_agent_pack_bundle(
    repo: str = Query(..., min_length=1, description="Target repo name (e.g. argus-cortex)"),
    token_budget: int = Query(
        default=DEFAULT_BUNDLE_TOKEN_BUDGET,
        ge=100,
        le=32000,
        description=f"Approx. token cap for briefs_markdown (~{CHARS_PER_TOKEN} chars/token)",
    ),
    q: str = Query(default="", description="Boost specialists matching id/title/path/kind"),
    limit_specialists: int = Query(default=5, ge=0, le=50),
    status: str = Query(
        default="open,claimed,in_progress",
        description="Specialist statuses to include (comma-separated)",
    ),
) -> JSONResponse:
    """One-call mission bundle: world model + ranked specialist packs + capped briefs."""
    from auditor.agent_pack import build_pack_bundle, index_path, rebuild_index
    from conductor.publish import public_base_url

    out = _output_dir()
    if not index_path(out).exists():
        rebuild_index(out, public_base_url=public_base_url())
    return JSONResponse(
        build_pack_bundle(
            out,
            repo=repo,
            token_budget=token_budget,
            q=q,
            limit_specialists=limit_specialists,
            status=status,
            public_base_url=public_base_url(),
        )
    )


class _PackStatusRequest(BaseModel):
    status: Literal["open", "claimed", "in_progress", "resolved", "wontfix"]
    note: str = ""


@app.post("/audit/packs/{pack_id}/status", dependencies=[Depends(_require_mutating_auth)])
def set_agent_pack_status(pack_id: str, request: _PackStatusRequest) -> JSONResponse:
    """Update a pack's lifecycle status (persisted in YAML and reflected in INDEX.json)."""
    from auditor.agent_pack import (
        AgentCodingPack,
        ingest_packs_to_rag,
        pack_rag_ingest_enabled,
        update_pack_status,
    )
    from conductor.publish import public_base_url

    updated = update_pack_status(
        _output_dir(),
        pack_id,
        request.status,
        request.note,
        public_base_url=public_base_url(),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found.")
    if pack_rag_ingest_enabled():
        # Refresh the slim RAG doc so retrieval reflects the new status (best-effort).
        try:
            ingest_packs_to_rag([AgentCodingPack.from_dict(updated)])
        except Exception as exc:
            logger.warning("pack status RAG refresh failed for %s: %s", pack_id, exc)
    return JSONResponse({
        "id": pack_id,
        "status": updated["status"],
        "status_note": updated["status_note"],
        "status_updated_at": updated["status_updated_at"],
        "draft_pr_url": (updated.get("repair") or {}).get("draft_pr_url"),
    })


@app.get("/audit/packs/{pack_id}")
def get_agent_pack(pack_id: str) -> JSONResponse:
    """Return the full agent coding pack for *pack_id*."""
    from auditor.agent_pack import load_pack

    data = load_pack(_output_dir(), pack_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found.")
    return JSONResponse(data)


@app.get("/audit/packs/{pack_id}/brief", response_class=PlainTextResponse)
def get_agent_pack_brief(pack_id: str) -> PlainTextResponse:
    """Markdown brief for a coding agent (also written as ``.md`` beside the YAML)."""
    from auditor.agent_pack import AgentCodingPack, load_pack, pack_file_path

    out = _output_dir()
    md_path = pack_file_path(out, pack_id).with_suffix(".md")
    if md_path.exists():
        return PlainTextResponse(md_path.read_text(encoding="utf-8"))
    data = load_pack(out, pack_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found.")
    pack = AgentCodingPack.from_dict(data, fallback_id=pack_id)
    return PlainTextResponse(pack.to_brief_md())


@app.post("/audit/ingest", dependencies=[Depends(_require_mutating_auth)])
def trigger_ingest() -> JSONResponse:
    """Push the latest audit briefings to the RAG ingest service (audit_docs collection)."""
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
