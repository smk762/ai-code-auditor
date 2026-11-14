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
    ("/ui/repos", "Repos"),
    ("/ui/runs", "Runs"),
    ("/ui/findings", "Findings"),
    ("/ui/packs", "Packs"),
    ("/ui/wiki", "Wiki"),
    ("/ui/pack-savings", "Pack savings"),
    ("/ui/night", "Night"),
    ("/ui/architecture", "Architecture"),
    ("/audit/usage", "Usage"),
    ("/ui/topology", "Topology"),
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


def render_page(filename: str, active: str, slots: dict[str, str] | None = None) -> str:
    """Read ``auditor/static/<filename>``, inject the shared nav at ``<!--#nav-->`` and fill
    any ``<!--#name-->`` slots with the given (already-escaped) HTML."""
    text = (STATIC_DIR / filename).read_text(encoding="utf-8")
    text = text.replace(NAV_MARKER, nav_html(active), 1)
    for name, value in (slots or {}).items():
        text = text.replace(f"<!--#{name}-->", value)
    return text


def report_names(output_dir: str | Path) -> list[str]:
    """Markdown reports directly under output_dir (the only files the report viewer serves)."""
    return sorted(p.name for p in Path(output_dir).glob("*.md") if p.is_file())


def render_report_page(name: str, output_dir: str | Path) -> str | None:
    """A report from output_dir rendered to HTML inside the shell; None if it isn't one."""
    if name not in report_names(output_dir):  # whitelist, so no path tricks
        return None
    path = Path(output_dir) / name
    modified = datetime.fromtimestamp(path.stat().st_mtime).isoformat(sep=" ", timespec="minutes")
    return render_page("report.html", "/ui/runs", {
        "title": html.escape(name),
        "meta": html.escape(f"{path.stat().st_size:,} bytes · written {modified} · overwritten by each run"),
        "content": render_markdown(path.read_text(encoding="utf-8", errors="replace")),
    })


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


def _latest_run() -> dict[str, Any] | None:
    from sqlalchemy import select

    from auditor.db import AuditRun
    from auditor.run_views import run_to_dict

    with _db_session() as s:
        row = s.execute(select(AuditRun).order_by(AuditRun.started_at.desc()).limit(1)).scalar_one_or_none()
        return run_to_dict(row) if row else None


def _latest_findings() -> dict[str, Any] | None:
    """Severity counts for the newest finished-ok run that has findings stored.

    Ecosystem runs store no findings rows (only repo audits do), so "the last ecosystem
    run" would always read zero.
    """
    from sqlalchemy import func, select

    from auditor.db import FindingRecord
    from auditor.run_views import run_to_dict
    from auditor.ui_runs import _resolve_run

    with _db_session() as s:
        row = _resolve_run(s, None)
        if row is None:
            return None
        rows = s.execute(
            select(FindingRecord.severity, func.count())
            .where(FindingRecord.run_id == row.run_id)
            .group_by(FindingRecord.severity)
        ).all()
        run = run_to_dict(row)
    counts = {sev: 0 for sev in SEVERITIES}
    for sev, n in rows:
        key = str(sev or "INFO").upper()
        counts[key] = counts.get(key, 0) + int(n)
    return {"run_id": run["run_id"], "pipeline": run["pipeline"], "started_at": run["started_at"],
            "repos": len(run["scanned_repos"]), "by_severity": counts, "total": sum(counts.values())}


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

    out["last_run"] = _try("runs", _latest_run)
    out["findings"] = _try("findings", _latest_findings)
    out["packs"] = _try("packs", lambda: _pack_counts(output_dir))
    out["spend"] = _try("spend", _month_spend)
    return out
