"""Data for the repo summary cards and the packs board of the web UI."""
from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select

from auditor import ui
from auditor.db import AuditRun, RepoRun

logger = logging.getLogger(__name__)

PACK_STATUSES = ui.PACK_STATUSES
SEVERITIES = ui.SEVERITIES
BOARD_PER_COLUMN = 50


def _sev_rank(sev: str) -> int:
    s = str(sev or "").upper()
    return SEVERITIES.index(s) if s in SEVERITIES else len(SEVERITIES)


def _pack_entries(output_dir: str | Path) -> list[dict[str, Any]]:
    from auditor.agent_pack import index_path

    path = index_path(output_dir)
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("packs") or [])


# ── repo cards ───────────────────────────────────────────────────────────────

def _commit_time(repo_path: Path, sha: str) -> str | None:
    """Committer date of *sha* from the local mirror (None if it isn't fetched yet)."""
    if not sha or not (repo_path / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(repo_path), "show", "-s", "--format=%cI", sha],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _mirror_head(repo_path: Path) -> str | None:
    """HEAD of the local mirror: the code the audits actually scanned."""
    if not (repo_path / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _violations_by_repo(output_dir: Path, repo_names: list[str]) -> dict[str, int] | None:
    """Per-repo architecture violation counts; None if no report exists yet.

    Prefers the JSON sidecar; reports written before it existed are parsed from the
    markdown headings (each starts with the violating repo's name).
    """
    side = output_dir / "architecture_violations.json"
    if side.is_file():
        data = json.loads(side.read_text(encoding="utf-8"))
        return dict(Counter(v.get("repo", "") for v in data.get("violations") or []))
    md = output_dir / "architecture_violations.md"
    if not md.is_file():
        return None
    names = sorted(repo_names, key=len, reverse=True)  # longest first: argus-cortex before argus
    counts: Counter = Counter()
    for heading in re.findall(r"^### (.+)$", md.read_text(encoding="utf-8"), re.MULTILINE):
        for name in names:
            if heading.startswith(name) and heading[len(name):len(name) + 1] in ("", ":", " "):
                counts[name] += 1
                break
    return dict(counts)


def _findings_by_repo() -> dict[str, Any] | None:
    """Per-repo severity counts and new-since-baseline counts from the newest run with findings."""
    from auditor.run_views import run_to_dict
    from auditor.ui_runs import _baseline, _delta, _findings, _resolve_run

    with ui._db_session() as s:
        row = _resolve_run(s, None)
        if row is None:
            return None
        run = run_to_dict(row)
        current = _findings(s, row.run_id)
        base = _baseline(s, row)
        prev = _findings(s, base.run_id) if base else []
    sev: dict[str, Counter] = defaultdict(Counter)
    for f in current:
        sev[f["repo"]][f["severity"]] += 1
    new: Counter = Counter()
    if base:
        for f in _delta(current, prev, set(run["scanned_repos"]))["new"]:
            new[f["repo"]] += 1
    return {"run": {"run_id": run["run_id"], "pipeline": run["pipeline"], "started_at": run["started_at"]},
            "has_baseline": base is not None,
            "by_repo": {r: {"by_severity": dict(c), "total": sum(c.values()), "new": new.get(r, 0)}
                        for r, c in sev.items()}}


def _last_audits() -> dict[str, dict[str, Any]]:
    with ui._db_session() as s:
        rows = s.execute(
            select(RepoRun.repo_name, RepoRun.status, RepoRun.updated_at, RepoRun.run_id, AuditRun.pipeline)
            .join(AuditRun, AuditRun.run_id == RepoRun.run_id)
            .order_by(RepoRun.updated_at.desc())
        ).all()
    out: dict[str, dict[str, Any]] = {}
    for name, status, at, run_id, pipeline in rows:
        out.setdefault(name, {"run_id": run_id, "pipeline": pipeline, "status": status,
                              "at": at.isoformat() if at else None})
    return out


def repos_data(output_dir: str | Path) -> dict[str, Any]:
    """One card per enabled repo. Each source degrades on its own (listed in ``errors``)."""
    from auditor.config import load_ecosystem_config
    from auditor.merge_poll import load_state, merge_poll_state_path

    out_dir = Path(output_dir)
    errors: list[str] = []

    def _try(section: str, fn, default):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — one broken source must not blank the page
            logger.warning("repos: %s unavailable: %s", section, exc)
            errors.append(f"{section}: {type(exc).__name__}: {exc}"[:300])
            return default

    repos = [r for r in load_ecosystem_config().repos if r.enabled]
    names = [r.name for r in repos]
    poll = _try("merge poll", lambda: load_state(merge_poll_state_path(out_dir)), {})
    tips = poll.get("repos") or {}
    audits = _try("audits", _last_audits, {})
    findings = _try("findings", _findings_by_repo, None)
    violations = _try("violations", lambda: _violations_by_repo(out_dir, names), None)
    entries = _try("packs", lambda: _pack_entries(out_dir), [])

    packs: dict[str, Counter] = defaultdict(Counter)
    open_high: Counter = Counter()
    for e in entries:
        if e.get("layer") == "world_model":
            continue
        st = str(e.get("status") or "open")
        packs[e.get("repo", "")][st] += 1
        if st == "open" and str(e.get("severity", "")).upper() in ("CRITICAL", "HIGH"):
            open_high[e.get("repo", "")] += 1

    cards = []
    for repo in repos:
        tip = tips.get(repo.name) or {}
        sha = str(tip.get("sha") or "")
        local = Path(repo.path) if repo.path and "://" not in repo.path else None
        f = (findings or {}).get("by_repo", {}).get(repo.name) if findings else None
        mirror = _mirror_head(local) if local else None
        cards.append({
            "name": repo.name,
            "github_url": f"https://github.com/{tip['slug']}" if tip.get("slug") else None,
            "tip": {
                "branch": tip.get("branch") or poll.get("branch"),
                "sha": sha or None,
                "committed_at": _commit_time(local, sha) if local else None,
                "checked_at": tip.get("checked_at"),
                # mirrors sync in the nightly rest window, so they can trail the polled tip
                "mirror_sha": mirror,
                "mirror_behind": bool(mirror and mirror != sha),
            } if sha else None,
            "last_audit": audits.get(repo.name),
            "findings": (f or {"by_severity": {}, "total": 0, "new": 0}) if findings else None,
            "packs": {st: packs[repo.name].get(st, 0) for st in PACK_STATUSES} | {"open_high": open_high[repo.name]},
            "violations": violations.get(repo.name, 0) if violations is not None else None,
        })
    return {
        "repos": cards,
        "findings_run": findings["run"] if findings else None,
        "findings_has_baseline": bool(findings and findings["has_baseline"]),
        "poll_updated_at": poll.get("updated_at"),
        "errors": errors,
    }


# ── packs board ──────────────────────────────────────────────────────────────

def packs_board(
    output_dir: str | Path,
    *,
    repo: str = "",
    layer: str = "",
    kind: str = "",
    severity: str = "",
    q: str = "",
    per_column: int = BOARD_PER_COLUMN,
) -> dict[str, Any]:
    """Packs grouped by lifecycle status, most severe then highest score first.

    Totals are after filtering; each column lists at most ``per_column`` cards. Facets
    cover every pack, so the filters can be read off them.
    """
    entries = _pack_entries(output_dir)
    facets = {
        "repos": sorted({e.get("repo", "") for e in entries if e.get("repo")}),
        "kinds": sorted({e.get("kind", "") for e in entries if e.get("kind")}),
        "layers": sorted({e.get("layer", "") for e in entries if e.get("layer")}),
    }
    sev_set = {x.strip().upper() for x in severity.split(",") if x.strip()}
    ql = q.strip().lower()
    rows = [
        e for e in entries
        if (not repo or e.get("repo") == repo)
        and (not layer or e.get("layer") == layer)
        and (not kind or e.get("kind") == kind)
        and (not sev_set or str(e.get("severity", "")).upper() in sev_set)
        and (not ql or any(ql in str(e.get(k, "")).lower() for k in ("id", "title", "file_path", "repo")))
    ]
    rows.sort(key=lambda e: (_sev_rank(e.get("severity", "")), -float(e.get("score") or 0), e.get("id", "")))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in rows:
        grouped[str(e.get("status") or "open")].append(e)

    def _card(e: dict[str, Any]) -> dict[str, Any]:
        from auditor.ui_runs import _display_path

        return {k: e.get(k) for k in ("id", "title", "repo", "kind", "layer", "severity", "score",
                                      "status_updated_at", "draft_pr_url", "detected_at")} | {
            "path": _display_path(e.get("repo", ""), e.get("file_path", "")) if e.get("file_path") else ""}

    statuses = PACK_STATUSES + sorted(set(grouped) - set(PACK_STATUSES))
    return {
        "columns": [{"status": st, "total": len(grouped.get(st, [])),
                     "packs": [_card(e) for e in grouped.get(st, [])[:per_column]]} for st in statuses],
        "total": len(rows),
        "facets": facets,
        "filters": {"repo": repo, "layer": layer, "kind": kind, "severity": severity, "q": q,
                    "per_column": per_column},
    }


def render_pack_page(output_dir: str | Path, pack_id: str) -> str | None:
    """Server-rendered pack page: header, brief (markdown → HTML), status history."""
    import html as _html

    from auditor.agent_pack import AgentCodingPack, load_pack

    data = load_pack(output_dir, pack_id)
    if data is None:
        return None
    pack = AgentCodingPack.from_dict(data)
    meta = {
        "id": pack.work_item_id,
        "status": pack.status or "open",
        "status_note": pack.status_note,
        "draft_pr_url": (pack.repair or {}).get("draft_pr_url") or "",
        "history": pack.status_history,
    }
    title = (pack.narrative or {}).get("title") or pack.work_item_id
    return ui.render_page("pack.html", "/ui/packs", {
        "title": _html.escape(title),
        # the page header already shows the title, so drop the brief's own "# …" heading
        "brief": ui.render_markdown(re.sub(r"\A# [^\n]*\n", "", pack.to_brief_md())),
        # JSON for the page script; every "<" is escaped so the data can't end the <script> block.
        "data": json.dumps(meta).replace("<", "\\u003c"),
    })
