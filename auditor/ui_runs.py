"""Data for the runs list, run summary and findings pages of the web UI.

Findings are compared between runs in two passes. First by ``(repo, file_path, finding_type,
title)``, leaving out line numbers because unrelated edits shift them. Then, because LLM findings
are reworded from run to run, a leftover "new" and "fixed" pair in the same repo, file and type
counts as the same issue when their titles share enough words (Jaccard ≥ 0.3, or ≥ 0.2 within
10 lines). On real nightly runs the second pass pairs ~30% of the exact-title churn, and spot
checks of the pairs were all the same issue. A run's baseline is the latest earlier
finished-ok run of the same pipeline that scanned at least one of the same repos, so a nightly
single-repo audit is compared with the last audit of that repo, not with whichever repo ran
the night before.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select

from auditor import ui
from auditor.db import AuditRun, FindingRecord, RepoRun, as_utc
from auditor.run_views import SUCCESS_STATUSES, repo_run_to_dict, run_to_dict

SEVERITIES = ui.SEVERITIES
BASELINE_LOOKBACK = 50
DELTA_LIST_LIMIT = 200
FINDINGS_LIMIT = 500

# Markdown reports each pipeline writes to output_dir (overwritten by the next run).
REPORTS_BY_PIPELINE: dict[str, list[str]] = {
    "repo-audit": ["nightly_repo_report.md", "security_report.md"],
    "ecosystem-audit": [
        "ecosystem_report.md", "architecture_violations.md", "pattern_proposals.md",
        "extraction_candidates.md", "nightly_repo_report.md", "security_report.md",
    ],
}
# A report file counts as "from this run" if written between start and finish (+ slack).
REPORT_SLACK = timedelta(minutes=10)


Key = tuple[str, str, str, str]


# Reworded-finding match (second pass); thresholds measured on real nightly runs.
REWORD_JACCARD = 0.3
REWORD_JACCARD_NEAR = 0.2
REWORD_NEAR_LINES = 10
_STOPWORDS = frozenset(
    "the and for with when that from into not are its but can any has was via all only which this "
    "than then their they use uses using without instead".split()
)


def _key(f: dict[str, Any]) -> Key:
    return (f["repo"], f["file_path"], f["type"], f["title"])


def _tokens(title: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9_]{3,}", title.lower())) - _STOPWORDS


def _reworded(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Title similarity if a and b look like the same issue reworded, else 0."""
    if (a["repo"], a["file_path"], a["type"]) != (b["repo"], b["file_path"], b["type"]):
        return 0.0
    ta, tb = _tokens(a["title"]), _tokens(b["title"])
    j = len(ta & tb) / max(1, len(ta | tb))
    if j >= REWORD_JACCARD or (j >= REWORD_JACCARD_NEAR and abs(a["line"] - b["line"]) <= REWORD_NEAR_LINES):
        return j
    return 0.0


def _display_path(repo: str, path: str) -> str:
    """Repo-relative path for display (stored paths are absolute paths on the audit host)."""
    marker = f"/{repo}/"
    return path.split(marker, 1)[1] if marker in path else path


def _finding_dict(r: FindingRecord) -> dict[str, Any]:
    return {
        "repo": r.repo,
        "file_path": r.file_path,
        "path": _display_path(r.repo, r.file_path),
        "line": r.line,
        "type": r.finding_type,
        "severity": str(r.severity or "INFO").upper(),
        "title": r.title,
        "source": r.source,
    }


def _findings(session, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(select(FindingRecord).where(FindingRecord.run_id == run_id)).scalars().all()
    return [_finding_dict(r) for r in rows]


def _sev_counts(findings: Iterable[dict[str, Any]]) -> dict[str, int]:
    c = Counter(f["severity"] for f in findings)
    return {s: c.get(s, 0) for s in SEVERITIES} | {k: v for k, v in c.items() if k not in SEVERITIES}


def _sev_rank(f: dict[str, Any]) -> int:
    return SEVERITIES.index(f["severity"]) if f["severity"] in SEVERITIES else len(SEVERITIES)


def _baseline(session, run: AuditRun) -> AuditRun | None:
    if run.started_at is None:
        return None
    repos = set((run.metadata_json or {}).get("scanned_repos") or [])
    candidates = session.execute(
        select(AuditRun)
        .where(
            AuditRun.pipeline == run.pipeline,
            AuditRun.status.in_(SUCCESS_STATUSES),
            AuditRun.started_at < run.started_at,
        )
        .order_by(AuditRun.started_at.desc())
        .limit(BASELINE_LOOKBACK)
    ).scalars().all()
    for c in candidates:
        theirs = set((c.metadata_json or {}).get("scanned_repos") or [])
        if not repos or not theirs or repos & theirs:
            return c
    return None


def _delta(current: list[dict[str, Any]], previous: list[dict[str, Any]], repos: set[str]) -> dict[str, Any]:
    """New/fixed findings. Only repos this run scanned count, so a repo it skipped isn't 'fixed'."""
    if repos:
        previous = [f for f in previous if f["repo"] in repos]
    cur_keys = {_key(f) for f in current}
    prev_keys = {_key(f) for f in previous}
    new = [f for f in current if _key(f) not in prev_keys]
    fixed = [f for f in previous if _key(f) not in cur_keys]
    # Second pass: pair reworded findings greedily, best title match first.
    by_file: dict[tuple, list[int]] = defaultdict(list)
    for i, f in enumerate(fixed):
        by_file[(f["repo"], f["file_path"], f["type"])].append(i)
    candidates = sorted(
        ((score, ni, fi) for ni, n in enumerate(new)
         for fi in by_file.get((n["repo"], n["file_path"], n["type"]), [])
         if (score := _reworded(n, fixed[fi])) > 0),
        reverse=True,
    )
    paired_new: set[int] = set()
    paired_fixed: set[int] = set()
    for _score, ni, fi in candidates:
        if ni not in paired_new and fi not in paired_fixed:
            paired_new.add(ni)
            paired_fixed.add(fi)
    return {
        "new": [f for i, f in enumerate(new) if i not in paired_new],
        "fixed": [f for i, f in enumerate(fixed) if i not in paired_fixed],
        "reworded": len(paired_new),
    }


def _resolve_run(session, run_id: str | None) -> AuditRun | None:
    if run_id:
        return session.execute(select(AuditRun).where(AuditRun.run_id == run_id)).scalar_one_or_none()
    # Default: newest finished-ok run that has findings stored (ecosystem runs store none).
    return session.execute(
        select(AuditRun)
        .where(
            AuditRun.status.in_(SUCCESS_STATUSES),
            AuditRun.run_id.in_(select(FindingRecord.run_id).distinct()),
        )
        .order_by(AuditRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


# ── runs list ────────────────────────────────────────────────────────────────

def runs_data(pipeline: str = "", status: str = "", limit: int = 100) -> dict[str, Any]:
    with ui._db_session() as s:
        q = select(AuditRun).order_by(AuditRun.started_at.desc()).limit(limit)
        if pipeline:
            q = q.where(AuditRun.pipeline == pipeline)
        if status:
            q = q.where(AuditRun.status.in_([x.strip() for x in status.split(",") if x.strip()]))
        runs = [run_to_dict(r) for r in s.execute(q).scalars().all()]
        facets = s.execute(select(AuditRun.pipeline, AuditRun.status, func.count())
                           .group_by(AuditRun.pipeline, AuditRun.status)).all()
        stored = dict(s.execute(
            select(FindingRecord.run_id, func.count())
            .where(FindingRecord.run_id.in_([r["run_id"] for r in runs]))
            .group_by(FindingRecord.run_id)
        ).all()) if runs else {}
    pipelines: Counter = Counter()
    statuses: Counter = Counter()
    for p, st, n in facets:
        pipelines[p] += n
        statuses[st] += n
    for r in runs:
        r["findings_stored"] = int(stored.get(r["run_id"], 0))
    return {
        "runs": runs,
        "pipelines": dict(sorted(pipelines.items())),
        "statuses": dict(sorted(statuses.items())),
        "filters": {"pipeline": pipeline, "status": status, "limit": limit},
    }


# ── run summary ──────────────────────────────────────────────────────────────

def _reports(run: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    started = datetime.fromisoformat(run["started_at"]) if run["started_at"] else None
    finished = datetime.fromisoformat(run["finished_at"]) if run["finished_at"] else None
    out = []
    for name in REPORTS_BY_PIPELINE.get(run["pipeline"], []):
        path = output_dir / name
        if not path.is_file():
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        from_this_run = bool(
            started and finished
            and as_utc(started) - REPORT_SLACK <= mtime <= as_utc(finished) + REPORT_SLACK
        )
        out.append({"name": name, "url": f"/ui/reports/{name}", "modified_at": mtime.isoformat(timespec="seconds"),
                    "from_this_run": from_this_run})
    return out


def run_summary(run_id: str, output_dir: str | Path) -> dict[str, Any] | None:
    with ui._db_session() as s:
        row = s.execute(select(AuditRun).where(AuditRun.run_id == run_id)).scalar_one_or_none()
        if row is None:
            return None
        run = run_to_dict(row)
        repos = [repo_run_to_dict(r) for r in s.execute(
            select(RepoRun).where(RepoRun.run_id == run_id).order_by(RepoRun.updated_at.asc())
        ).scalars().all()]
        current = _findings(s, run_id)
        base = _baseline(s, row)
        previous = run_to_dict(base) if base else None
        prev_findings = _findings(s, base.run_id) if base else []

    findings: dict[str, Any] = {"stored": len(current), "by_severity": _sev_counts(current)}
    by_repo: dict[str, Counter] = defaultdict(Counter)
    for f in current:
        by_repo[f["repo"]][f["severity"]] += 1
    findings["by_repo"] = {r: dict(c) for r, c in sorted(by_repo.items())}
    if base:
        d = _delta(current, prev_findings, set(run["scanned_repos"]))
        prev_counts = _sev_counts(f for f in prev_findings if not run["scanned_repos"] or f["repo"] in run["scanned_repos"])
        findings["delta"] = {
            "new": len(d["new"]),
            "fixed": len(d["fixed"]),
            "reworded": d["reworded"],
            "by_severity": {sev: findings["by_severity"].get(sev, 0) - prev_counts.get(sev, 0) for sev in SEVERITIES},
            "new_findings": sorted(d["new"], key=_sev_rank)[:DELTA_LIST_LIMIT],
            "fixed_findings": sorted(d["fixed"], key=_sev_rank)[:DELTA_LIST_LIMIT],
        }
    return {
        "run": run,
        "repos": repos,
        "previous": previous,
        "findings": findings,
        "reports": _reports(run, Path(output_dir)),
        "combined_report_url": f"/audit/reports/{run_id}" if run["status"] in SUCCESS_STATUSES else None,
    }


# ── findings ─────────────────────────────────────────────────────────────────

def findings_data(
    run_id: str = "",
    *,
    repo: str = "",
    severity: str = "",
    source: str = "",
    type: str = "",  # noqa: A002 — query-parameter name
    q: str = "",
    change: str = "all",
    limit: int = FINDINGS_LIMIT,
) -> dict[str, Any] | None:
    """Findings for one run (default: newest finished run with findings), filtered.

    ``change``: ``all`` (this run), ``new`` (not in the baseline run) or ``fixed`` (in the
    baseline, gone now). The severity × repo matrix and facets cover the whole run, not the
    filtered rows, so the filters can be read off them.
    """
    with ui._db_session() as s:
        row = _resolve_run(s, run_id or None)
        if row is None:
            return None if run_id else {"run": None, "previous": None, "matrix": {}, "facets": {},
                                        "findings": [], "total": 0, "filtered_total": 0, "delta": None}
        run = run_to_dict(row)
        current = _findings(s, row.run_id)
        base = _baseline(s, row)
        previous = run_to_dict(base) if base else None
        prev_findings = _findings(s, base.run_id) if base else []

    d = _delta(current, prev_findings, set(run["scanned_repos"])) if base else None
    new_keys = {_key(f) for f in d["new"]} if d else set()
    for f in current:
        f["is_new"] = bool(d) and _key(f) in new_keys

    matrix: dict[str, dict[str, int]] = defaultdict(lambda: {s: 0 for s in SEVERITIES})
    for f in current:
        matrix[f["repo"]][f["severity"]] = matrix[f["repo"]].get(f["severity"], 0) + 1
    facets = {
        "repos": sorted(matrix),
        "sources": sorted({f["source"] for f in current}),
        "types": sorted({f["type"] for f in current}),
    }

    if change == "fixed":
        rows = list(d["fixed"]) if d else []
    elif change == "new":
        rows = list(d["new"]) if d else []
    else:
        rows = current
    sev_set = {x.strip().upper() for x in severity.split(",") if x.strip()}
    ql = q.strip().lower()
    rows = [
        f for f in rows
        if (not repo or f["repo"] == repo)
        and (not sev_set or f["severity"] in sev_set)
        and (not source or f["source"] == source)
        and (not type or f["type"] == type)
        and (not ql or ql in f["title"].lower() or ql in f["path"].lower())
    ]
    rows.sort(key=lambda f: (_sev_rank(f), f["repo"], f["file_path"], f["line"]))
    return {
        "run": run,
        "previous": previous,
        "matrix": {r: matrix[r] for r in facets["repos"]},
        "facets": facets,
        "delta": {"new": len(d["new"]), "fixed": len(d["fixed"]), "reworded": d["reworded"]} if d else None,
        "total": len(current),
        "filtered_total": len(rows),
        "findings": rows[:limit],
        "filters": {"repo": repo, "severity": severity, "source": source, "type": type, "q": q,
                    "change": change, "limit": limit},
    }
