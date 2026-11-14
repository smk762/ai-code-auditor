"""Rest-session wrap-up: atomic publish, webhook, slim RAG ingest."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from conductor.briefing import build_briefing
from conductor.publish import latest_urls, publish_status, public_base_url
from conductor.schemas import (
    DecisionNeeded,
    DraftPr,
    FindingsSummary,
    MithrandirStatus,
    RestWindowInfo,
    TopFinding,
)
from conductor.webhook import notify_mithrandir

logger = logging.getLogger(__name__)


def build_status(
    *,
    run_id: str,
    ended_reason: str,
    window: RestWindowInfo,
    repos: list[str],
    scanner_counts: dict[str, int],
    findings: FindingsSummary,
    draft_prs: list[DraftPr],
    decisions_needed: list[DecisionNeeded],
    worker_stats: dict[str, Any],
    cost_usd_est: float = 0.0,
    next_rest_window: str = "",
    summary_one_liner: str | None = None,
) -> MithrandirStatus:
    json_url, md_url = latest_urls()
    one = summary_one_liner or _default_one_liner(ended_reason, findings, draft_prs, decisions_needed)
    return MithrandirStatus(
        run_id=run_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        ended_reason=ended_reason,  # type: ignore[arg-type]
        summary_one_liner=one,
        speakable=True,
        latest_url=json_url,
        latest_md_url=md_url,
        window=window,
        repos=repos,
        scanner_counts=scanner_counts,
        findings=findings,
        draft_prs=draft_prs,
        decisions_needed=decisions_needed,
        worker_stats=worker_stats,
        cost_usd_est=cost_usd_est,
        next_rest_window=next_rest_window,
    )


def _default_one_liner(
    ended_reason: str,
    findings: FindingsSummary,
    draft_prs: list[DraftPr],
    decisions: list[DecisionNeeded],
) -> str:
    parts = [
        f"Rest {ended_reason}: {findings.confirmed} confirmed, {len(draft_prs)} draft PRs",
    ]
    if decisions:
        parts.append(f"{len(decisions)} decision(s)")
    return ", ".join(parts)[:120]


def wrap_up(
    status: MithrandirStatus,
    *,
    output_dir: str | Path = "reports",
    send_webhook: bool = True,
    ingest_rag: bool = True,
) -> dict[str, Any]:
    if not status.pr_brief:
        status.pr_brief = _cached_pr_brief(output_dir)
    md, speakable = build_briefing(status)
    status.speakable = speakable
    status.body_md = md
    published = publish_status(status, md, output_dir=output_dir)

    webhook_result: dict[str, Any] = {"sent": False, "skipped": True}
    if send_webhook:
        webhook_result = notify_mithrandir(status=published.status)

    rag_result: dict[str, Any] = {"ingested": False, "skipped": True}
    if ingest_rag:
        rag_result = ingest_latest_to_rag(published.status, md)

    return {
        "json_path": str(published.json_path),
        "md_path": str(published.md_path),
        "status": published.status,
        "webhook": webhook_result,
        "rag": rag_result,
        "public_base_url": public_base_url(),
    }


def _cached_pr_brief(output_dir: str | Path) -> str:
    """Spoken blocked-PR summary from the PR-intel cache, if it is under 6 hours old."""
    from auditor.pr_intel import load_report, pr_intel_enabled

    if not pr_intel_enabled():
        return ""
    report = load_report(output_dir, max_age_h=6)
    return str((report or {}).get("spoken") or "")


def ingest_latest_to_rag(status: dict[str, Any], body_md: str) -> dict[str, Any]:
    """Ingest LATEST.md + short JSON summary only — not full SARIF.

    Embeddings should use Jarrah Ollama (http://192.168.1.109:11434) via rag-ingest config.
    """
    import json

    from auditor.rag_client import post_documents

    short = {
        "event_id": status.get("event_id"),
        "run_id": status.get("run_id"),
        "severity": status.get("severity"),
        "requires_ack": status.get("requires_ack"),
        "summary_one_liner": status.get("summary_one_liner"),
        "ended_reason": status.get("ended_reason"),
        "findings": status.get("findings"),
        "draft_prs": [
            {"repo": p.get("repo"), "url": p.get("url"), "action": p.get("action")}
            for p in (status.get("draft_prs") or [])
        ],
    }
    text = body_md.strip() + "\n\n---\n" + json.dumps(short, indent=2)
    doc = {"id": f"auditor-rest-{status.get('run_id', 'unknown')}",
           "title": f"Night conductor: {status.get('summary_one_liner') or status.get('run_id', '')}", "text": text}
    r = post_documents([doc], tags=["kind:mithrandir_latest"])
    return {k: v for k, v in r.items() if k in ("ingested", "skipped", "status_code", "error")}


def top_findings_from_worker_results(results: list[dict[str, Any]], limit: int = 3) -> list[TopFinding]:
    tops: list[TopFinding] = []
    for r in results:
        for f in r.get("findings") or []:
            if not isinstance(f, dict):
                continue
            tops.append(
                TopFinding(
                    repo=str(f.get("repo") or r.get("repo") or ""),
                    severity=str(f.get("severity") or "MEDIUM"),
                    title=str(f.get("title") or f.get("symbol") or "finding")[:160],
                    pr_url=f.get("pr_url"),
                )
            )
            if len(tops) >= limit:
                return tops
    return tops
