"""Web UI shell for audit-api: shared nav partial, page rendering, markdown, landing summary.

Pages are static HTML under ``auditor/static/`` that link ``/audit/static/ui.css`` and
``ui.js``. The server injects the nav at the ``<!--#nav-->`` marker so every page shares one
header, and each page fetches its own JSON endpoint. Markdown is rendered here, in Python,
so the internal UI and the public wiki export share one renderer (no client-side markdown).
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_URL = "/audit/static"
NAV_MARKER = "<!--#nav-->"

# (path, label). Order is the nav order.
NAV_ITEMS: list[tuple[str, str]] = [
    ("/", "Overview"),
    ("/audit/usage", "Usage"),
    ("/audit/topology", "Topology"),
    ("/docs", "API"),
]

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
PACK_STATUSES = ["open", "claimed", "in_progress", "resolved", "wontfix"]


# ── nav + pages ──────────────────────────────────────────────────────────────

def nav_html(active: str) -> str:
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="{path}"{current if path == active else ""}>{html.escape(label)}</a>'
        for path, label in NAV_ITEMS
    )
    return (
        '<nav class="ui-nav" aria-label="Main"><div class="inner">'
        '<a class="brand" href="/">ai-code-auditor</a>'
        f'<div class="links">{links}</div>'
        '<button class="theme" id="ui-theme" type="button">Theme: auto</button>'
        "</div></nav>"
    )


def render_page(filename: str, active: str) -> str:
    """Read ``auditor/static/<filename>`` and inject the shared nav at ``<!--#nav-->``."""
    text = (STATIC_DIR / filename).read_text(encoding="utf-8")
    return text.replace(NAV_MARKER, nav_html(active), 1)


# ── markdown ─────────────────────────────────────────────────────────────────

_md = MarkdownIt("commonmark", {"html": False}).enable("table")
_ALIGN = re.compile(r"text-align:\s*(left|center|right)")


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", s) or "section"


def render_markdown(text: str) -> str:
    """Render markdown to HTML that is safe to embed and CSP-neutral.

    Raw HTML in the source is escaped, unsafe link schemes are refused (markdown-it's
    ``validateLink``), table alignment becomes ``align-*`` classes instead of inline
    ``style`` attributes, and headings get stable, de-duplicated ``id`` anchors.
    """
    tokens: list[Token] = _md.parse(text or "")
    seen: dict[str, int] = {}
    for i, tok in enumerate(tokens):
        if tok.type in ("th_open", "td_open"):
            style = tok.attrGet("style")
            if style is not None:
                tok.attrs.pop("style", None)
                m = _ALIGN.search(str(style))
                if m:
                    tok.attrSet("class", f"align-{m.group(1)}")
        elif tok.type == "heading_open" and i + 1 < len(tokens):
            base = _slug(tokens[i + 1].content)
            n = seen.get(base, 0)
            seen[base] = n + 1
            tok.attrSet("id", base if n == 0 else f"{base}-{n}")
    return _md.renderer.render(tokens, _md.options, {})


# ── landing summary (/audit/summary.json) ────────────────────────────────────

def _db_session():
    from auditor.db import db_session

    return db_session()


def _last_runs() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(latest run of any pipeline, latest successful ecosystem-audit run)."""
    from sqlalchemy import select

    from auditor.db import AuditRun

    def _row(r: AuditRun | None) -> dict[str, Any] | None:
        if r is None:
            return None
        meta = r.metadata_json or {}
        return {
            "run_id": r.run_id,
            "pipeline": r.pipeline,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "duration_ms": meta.get("duration_ms"),
            "findings": int(meta.get("findings", 0) or 0),
            "violations": int(meta.get("violations", 0) or 0),
            "repos": len(meta.get("scanned_repos") or []),
        }

    with _db_session() as s:
        latest = s.execute(select(AuditRun).order_by(AuditRun.started_at.desc()).limit(1)).scalar_one_or_none()
        eco = s.execute(
            select(AuditRun)
            .where(AuditRun.pipeline == "ecosystem-audit", AuditRun.status == "success")
            .order_by(AuditRun.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _row(latest), _row(eco)


def _findings_by_severity(run_id: str) -> dict[str, int]:
    from sqlalchemy import func, select

    from auditor.db import FindingRecord

    with _db_session() as s:
        rows = s.execute(
            select(FindingRecord.severity, func.count())
            .where(FindingRecord.run_id == run_id)
            .group_by(FindingRecord.severity)
        ).all()
    counts = {sev: 0 for sev in SEVERITIES}
    for sev, n in rows:
        key = str(sev or "INFO").upper()
        counts[key] = counts.get(key, 0) + int(n)
    return counts


def _pack_counts(output_dir: str | Path) -> dict[str, Any]:
    from auditor.agent_pack import index_path

    path = index_path(output_dir)
    if not path.exists():
        return {"total": 0, "status_counts": {s: 0 for s in PACK_STATUSES}, "updated_at": None}
    idx = json.loads(path.read_text(encoding="utf-8"))
    counts = {s: 0 for s in PACK_STATUSES}
    counts.update({str(k): int(v) for k, v in (idx.get("status_counts") or {}).items()})
    return {"total": int(idx.get("count") or 0), "status_counts": counts, "updated_at": idx.get("updated_at")}


def _month_spend() -> dict[str, Any]:
    from auditor.spend_ledger import budget_month_start, get_ledger

    led = get_ledger()
    return {
        "spent": round(led.spent(), 4),
        "cap": led.caps().get("total"),
        "month_start": budget_month_start().isoformat(),
    }


def summary_data(output_dir: str | Path) -> dict[str, Any]:
    """Headline numbers for the landing page.

    Each section is loaded independently: a missing DB or pack index degrades that
    section to ``None`` and is listed in ``errors`` rather than failing the page.
    """
    out: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "last_run": None,
        "last_ecosystem_run": None,
        "findings": None,
        "packs": None,
        "spend": None,
        "errors": [],
    }

    def _try(section: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — one broken source must not blank the page
            logger.warning("summary: %s unavailable: %s", section, exc)
            out["errors"].append(f"{section}: {type(exc).__name__}: {exc}"[:300])
            return None

    runs = _try("runs", _last_runs)
    if runs:
        out["last_run"], out["last_ecosystem_run"] = runs
    eco = out["last_ecosystem_run"]
    if eco:
        by_sev = _try("findings", lambda: _findings_by_severity(eco["run_id"]))
        if by_sev is not None:
            out["findings"] = {"run_id": eco["run_id"], "by_severity": by_sev, "total": sum(by_sev.values())}
    out["packs"] = _try("packs", lambda: _pack_counts(output_dir))
    out["spend"] = _try("spend", _month_spend)
    return out
