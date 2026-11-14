"""The "Notable findings" page, built from data (findings DB, packs, violations) without an LLM.

Cheap and always current: it is regenerated on every build, even when no page is redrafted.
Citations use the same [path:Lx-Ly] form as the generated pages so the viewer links them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
MAX_FINDINGS = 25
MAX_PACKS = 15


def _esc(text: Any) -> str:
    """Keep table cells on one line and stop stray pipes breaking the table."""
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _cite(repo: str, path: str, line: int) -> str:
    from auditor.ui_runs import _display_path

    rel = _display_path(repo, path)
    return f"[{rel}:L{line}]" if line else f"`{rel}`"


def _findings(repo: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    from auditor import ui
    from auditor.run_views import run_to_dict
    from auditor.ui_runs import _findings as run_findings, _resolve_run

    with ui._db_session() as s:
        row = _resolve_run(s, None)
        if row is None:
            return None, [], 0
        run = run_to_dict(row)
        rows = [f for f in run_findings(s, row.run_id) if f["repo"] == repo]
    rows.sort(key=lambda f: (SEVERITIES.index(f["severity"]) if f["severity"] in SEVERITIES else 9, f["path"], f["line"]))
    return run, rows, len(rows)


def build_findings_page(repo: str, output_dir: str | Path) -> str:
    from auditor.ui_arch import _violations
    from auditor.ui_repos import _pack_entries

    lines = ["# Notable findings", "",
             f"Built from the audit data on {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC, "
             "not written by a model. It is refreshed on every wiki build.", ""]

    try:
        run, rows, total = _findings(repo)
    except Exception as exc:  # noqa: BLE001 — the page still renders the other sections
        run, rows, total = None, [], 0
        lines += [f"_Findings unavailable: {type(exc).__name__}._", ""]
    lines += ["## Open findings", ""]
    if run is None:
        lines += ["No audit run has findings stored yet.", ""]
    elif not rows:
        lines += [f"None in the latest audit ({run['run_id']}).", ""]
    else:
        counts = {s: sum(1 for f in rows if f["severity"] == s) for s in SEVERITIES}
        lines += [f"{total} in the latest audit ({run['run_id']}): "
                  + ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n) + ".", "",
                  "| Severity | Finding | Where | Source |", "|---|---|---|---|"]
        for f in rows[:MAX_FINDINGS]:
            lines.append(f"| {f['severity'].lower()} | {_esc(f['title'])} | {_cite(repo, f['file_path'], f['line'])} | {_esc(f['source'])} |")
        if total > MAX_FINDINGS:
            lines += ["", f"Showing the {MAX_FINDINGS} most severe of {total}; the rest are on the Findings page."]
        lines.append("")

    packs = [e for e in _pack_entries(output_dir)
             if e.get("repo") == repo and e.get("layer") != "world_model" and e.get("status") in ("open", "claimed", "in_progress")]
    packs.sort(key=lambda e: (SEVERITIES.index(str(e.get("severity", "")).upper()) if str(e.get("severity", "")).upper() in SEVERITIES else 9,
                              -float(e.get("score") or 0)))
    lines += ["## Agent coding packs in flight", ""]
    if not packs:
        lines += ["No open packs.", ""]
    else:
        lines += [f"{len(packs)} open, claimed or in progress.", "", "| Severity | Status | Pack |", "|---|---|---|"]
        for e in packs[:MAX_PACKS]:
            lines.append(f"| {str(e.get('severity', '')).lower()} | {_esc(e.get('status'))} | {_esc(e.get('title') or e.get('id'))} (`{e.get('id')}`) |")
        lines.append("")

    v = _violations(Path(output_dir), [repo])
    mine = [x for x in (v or {}).get("violations", []) if x["repo"] == repo]
    lines += ["## Architecture violations", ""]
    if v is None:
        lines += ["No architecture report yet.", ""]
    elif not mine:
        lines += ["None in the latest ecosystem run.", ""]
    else:
        lines += ["| Severity | Rule | Where |", "|---|---|---|"]
        for x in mine:
            lines.append(f"| {x['severity'].lower()} | {_esc(x['rule'].replace('_', ' '))} | {_esc(x['path'])} |")
        lines.append("")
    return "\n".join(lines)
