"""Data for the conductor / night summary page.

A rest session leaves ``reports/conductor/<run_id>/<task>.json`` (one file per mission: the
task and its result) and a mithrandir record (``reports/mithrandir/LATEST.json`` and
``archive/*.json``) with decisions, draft PRs and scanner counts. Mission outcomes are taken
from the mission files: the mithrandir worker stats don't count ``PARTIAL`` missions, and its
``cost_usd_est`` is not filled in, so spend comes from the spend ledger's ``conductor`` lane
inside the session window instead.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSION_RE = re.compile(r"^rest-(\d{8}T\d{6})-[0-9a-f]+$")
MISSION_STATUSES = ["COMPLETED", "PARTIAL", "FAILED", "ESCALATED"]
# Spend is attributed to a session from its start until the next session (at most this long).
MAX_SESSION_SPAN = timedelta(hours=12)
HISTORY_LIMIT = 30


def _session_start(run_id: str) -> datetime | None:
    m = SESSION_RE.match(run_id)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("night: skip unreadable %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _mithrandir_records(output_dir: Path) -> dict[str, dict[str, Any]]:
    """run_id → mithrandir record (archive first, LATEST wins for its run)."""
    root = output_dir / "mithrandir"
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "archive").glob("*.json")) + [root / "LATEST.json"]:
        if path.is_file() and (rec := _read_json(path)) and rec.get("run_id"):
            out[str(rec["run_id"])] = rec
    return out


def _mission(path: Path) -> dict[str, Any] | None:
    data = _read_json(path)
    if data is None:
        return None
    task, result = data.get("task") or {}, data.get("result") or {}
    task_id = str(task.get("id") or result.get("task_id") or path.stem)
    mission = str(task.get("mission") or "")
    # task ids are "<repo>-<mission>-<nnn>"
    repo = task_id.rsplit(f"-{mission}-", 1)[0] if mission and f"-{mission}-" in task_id else task_id
    findings = [f for f in (result.get("findings") or []) if isinstance(f, dict)]
    return {
        "id": task_id,
        "repo": repo,
        "mission": mission,
        "title": task.get("title") or task_id,
        "status": str(result.get("status") or "UNKNOWN").upper(),
        "summary": str(result.get("summary") or ""),
        "escalate_reason": str(result.get("escalate_reason") or ""),
        "findings": [
            {k: f.get(k) for k in ("id", "title", "severity", "category", "confidence", "evidence", "recommendation")}
            for f in findings
        ],
    }


def _problems(missions: list[dict[str, Any]], rec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Things a human should look at, grouped so 12 identical failures read as one line."""
    reasons: dict[str, list[str]] = {}
    for m in missions:
        if m["status"] in ("FAILED", "ESCALATED"):
            reason = (m["escalate_reason"] or m["summary"] or "no reason recorded").strip().splitlines()[0][:300]
            reasons.setdefault(f"{m['status'].lower()}: {reason}", []).append(m["id"])
        elif re.search(r"\bnot (available|found)\b", m["summary"], re.IGNORECASE):
            reasons.setdefault(f"incomplete: {m['summary'].strip().splitlines()[0][:300]}", []).append(m["id"])
    out = [{"kind": "mission", "text": text, "missions": ids} for text, ids in reasons.items()]
    for d in (rec or {}).get("decisions_needed") or []:
        out.append({"kind": "decision", "text": str(d.get("summary") or "").strip()[:400],
                    "repo": d.get("repo"), "id": d.get("id")})
    return out


def _spend(start: datetime, end: datetime) -> dict[str, Any]:
    from auditor.spend_ledger import get_ledger

    rows = get_ledger().rows_since(start.timestamp())
    lane = [r for r in rows if r[1] == "conductor" and r[0] < end.timestamp()]
    return {"usd": round(sum(r[7] for r in lane), 4), "calls": len(lane),
            "input_tokens": sum(r[4] for r in lane), "output_tokens": sum(r[5] for r in lane)}


def _session(output_dir: Path, run_dir: Path, rec: dict[str, Any] | None, end: datetime | None,
             *, detail: bool) -> dict[str, Any]:
    missions = [m for p in sorted(run_dir.glob("*.json")) if (m := _mission(p))]
    counts = Counter(m["status"] for m in missions)
    start = _session_start(run_dir.name)
    findings = (rec or {}).get("findings") or {}
    spend = None
    if start:
        try:
            spend = _spend(start, min(end or start + MAX_SESSION_SPAN, start + MAX_SESSION_SPAN))
        except Exception as exc:  # noqa: BLE001 — ledger trouble must not hide the night
            logger.warning("night: spend unavailable: %s", exc)
    out: dict[str, Any] = {
        "run_id": run_dir.name,
        "started_at": start.isoformat() if start else None,
        "missions_total": len(missions),
        "mission_counts": {s: counts.get(s, 0) for s in MISSION_STATUSES} | {
            s: n for s, n in counts.items() if s not in MISSION_STATUSES},
        "findings_reported": sum(len(m["findings"]) for m in missions),
        "confirmed": findings.get("confirmed"),
        "dismissed": findings.get("dismissed"),
        "manual_review": findings.get("manual_review"),
        "draft_prs": list((rec or {}).get("draft_prs") or []),
        "decisions_needed": len((rec or {}).get("decisions_needed") or []),
        "ended_reason": (rec or {}).get("ended_reason"),
        "headline": (rec or {}).get("summary_one_liner"),
        "spend": spend,
    }
    if detail:
        out |= {
            "missions": missions,
            "problems": _problems(missions, rec),
            "window": (rec or {}).get("window"),
            "repos": (rec or {}).get("repos") or [],
            "scanner_counts": (rec or {}).get("scanner_counts") or {},
            "top_findings": ((rec or {}).get("findings") or {}).get("top") or [],
            "briefing_md": (rec or {}).get("body_md") or "",
            "mithrandir_generated_at": (rec or {}).get("generated_at"),
        }
    return out


def night_data(output_dir: str | Path, run_id: str = "") -> dict[str, Any] | None:
    """The selected session (default: newest) in detail, plus a short history of sessions.

    Returns None only when ``run_id`` is given and doesn't exist.
    """
    out_dir = Path(output_dir)
    root = out_dir / "conductor"
    runs = sorted((p for p in root.glob("rest-*") if p.is_dir() and SESSION_RE.match(p.name)),
                  key=lambda p: p.name, reverse=True) if root.is_dir() else []
    if run_id and run_id not in {p.name for p in runs}:
        return None
    records = _mithrandir_records(out_dir)
    selected = next((p for p in runs if p.name == run_id), runs[0] if runs else None)
    # each session's spend window ends where the next (newer) one starts
    ends = {runs[i].name: _session_start(runs[i - 1].name) for i in range(1, len(runs))}
    history = [_session(out_dir, p, records.get(p.name), ends.get(p.name), detail=False)
               for p in runs[:HISTORY_LIMIT]]
    return {
        "session": _session(out_dir, selected, records.get(selected.name), ends.get(selected.name), detail=True)
        if selected else None,
        "history": history,
    }
