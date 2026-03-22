"""FastAPI service exposing the ai-code-auditor as a submit-poll HTTP API.

Endpoints
---------
GET  /audit/health              VRAM info + service readiness (gothmog capacity check)
POST /audit/run                 Start a new audit run; returns {run_id} immediately
GET  /audit/runs                List recent runs from PostgreSQL (newest first)
GET  /audit/runs/{run_id}       Poll run status and results
GET  /audit/reports/{run_id}    Return combined markdown report for a completed run
POST /audit/ingest              Trigger RAG briefing ingest to mimiri/audit_docs

Start:
    .venv/bin/uvicorn auditor.audit_api:app --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.config import load_ecosystem_config
from auditor.db import AuditRun, db_session, utcnow
from auditor.gpu_scheduler import get_vram_info

logger = logging.getLogger(__name__)

app = FastAPI(title="ai-code-auditor API", version="1.0.0")

# ---------------------------------------------------------------------------
# Active-run tracking (in-memory, single slot)
# ---------------------------------------------------------------------------

_active_run_id: str | None = None
_active_run_lock = threading.Lock()


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
            if status in ("completed", "failed"):
                row.finished_at = utcnow()


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


def _row_to_dict(row: AuditRun) -> dict[str, Any]:
    meta: dict = row.metadata_json or {}
    return {
        "run_id":        row.run_id,
        "status":        row.status,
        "started_at":    row.started_at.isoformat() if row.started_at else None,
        "finished_at":   row.finished_at.isoformat() if row.finished_at else None,
        "scanned_repos": meta.get("scanned_repos", []),
        "scanned_files": meta.get("scanned_files", 0),
        "findings":      meta.get("findings", 0),
        "violations":    meta.get("violations", 0),
        "errors":        meta.get("errors", []),
        "duration_ms":   meta.get("duration_ms"),
        "stage_timings_ms": meta.get("stage_timings_ms", {}),
        "extra":         meta.get("extra", {}),
    }


# ---------------------------------------------------------------------------
# Background pipeline worker
# ---------------------------------------------------------------------------

def _run_pipeline(run_id: str) -> None:
    global _active_run_id
    pipeline_script = Path(__file__).resolve().parents[1] / "pipelines" / "ecosystem_audit.py"
    env = {**os.environ, "AI_AUDIT_RUN_ID": run_id}

    try:
        _db_update_status(run_id, "running")
        result = subprocess.run(
            [sys.executable, str(pipeline_script)],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            _db_update_status(run_id, "completed")
        else:
            # Append stderr to the run's metadata_json errors list via a direct update.
            stderr_tail = result.stderr[-2000:] if result.stderr else ""
            with db_session() as session:
                row = session.execute(
                    select(AuditRun).where(AuditRun.run_id == run_id)
                ).scalar_one_or_none()
                if row is not None:
                    meta = dict(row.metadata_json or {})
                    errs = list(meta.get("errors", []))
                    errs.append(f"Pipeline exit {result.returncode}: {stderr_tail}")
                    meta["errors"] = errs
                    row.metadata_json = meta
                    row.status = "failed"
                    row.finished_at = utcnow()
    except Exception as exc:
        logger.exception("Audit pipeline thread raised: %s", exc)
        _db_update_status(run_id, "failed")
    finally:
        with _active_run_lock:
            _active_run_id = None


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


@app.get("/audit/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    """Return status and results for a specific run."""
    row = _db_get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")
    return JSONResponse(row)


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
    if row["status"] not in ("completed", "partial_success"):
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id!r} has status {row['status']!r}; no report yet.",
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
