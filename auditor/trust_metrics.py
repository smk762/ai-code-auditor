"""Trust metrics: can agent fixes be trusted, and is the scan steady enough to tell? (#52)

Every other part of the trust-upgrade epic (#64) is judged against these numbers.

=====================  ==============================================================
Metric                 Definition
=====================  ==============================================================
false_resolve          Share of validator resolutions where no tests ran
                       (``tool=lint_only``): the pack closed on a lint pass alone.
reopen                 Share of packs ever resolved that later went back to open,
                       claimed or in_progress.
recurrence             Share of resolved finding packs whose issue comes back. That is
                       either a later pack linked by ``governance.recurrence_of``, or a
                       later scan of the repo reporting the same file with a reworded
                       title (``finding_match``). A pack counts only once a later scan
                       of its repo has run ("observed"); until then it is pending.
scan_persistence       For consecutive scans of one repo, the share of the earlier
                       scan's findings the later scan reports again, leaving out those
                       whose pack was resolved in between. It measures the detector,
                       not the agents. When it is low, a missing finding doesn't prove
                       a fix, so read recurrence against it.
agent_prs              Merged PRs that fix audit packs (they reference a pack id or say
                       "ai-code-auditor" findings/packs), and the share reverted within
                       ``REVERT_WINDOW_DAYS``. A revert is a commit on the mirror's dev
                       that reverts one of the PR's commits, names the PR, or is titled
                       ``Revert "<PR title>"``. Fix-forwards are counted the same way,
                       as a later commit that names the PR and starts with "fix".
flow                   Per ISO week and finding type: findings new to a repo's scan and
                       findings gone since its previous scan (rewordings paired). Class
                       ids from #53 will replace the type.
=====================  ==============================================================

Everything is computed from local state: pack YAMLs, the findings DB and the git mirrors. The
GitHub API is only needed to list agent PRs. That list is cached in ``trust_prs.json`` and
refreshed with ``refresh_prs=True``. ``refresh()`` writes ``trust.json`` and appends the
headline to ``trust_history.jsonl``, which is never overwritten.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from auditor.finding_match import reworded_score

logger = logging.getLogger(__name__)

REPORT_NAME = "trust.json"
HISTORY_NAME = "trust_history.jsonl"
PRS_CACHE_NAME = "trust_prs.json"
REVERT_WINDOW_DAYS = 14

# Pack kinds that describe one finding. The others describe topology or proposals, not a bug.
NON_FINDING_KINDS = frozenset({"world_model", "architecture", "extraction", "invariants"})
REOPEN_STATUSES = frozenset({"open", "claimed", "in_progress"})
_TOOL_RE = re.compile(r"\btool=([a-z_+]+)")
_EVIDENCE_RE = re.compile(r"\bevidence=([a-z_]+)")
PACK_ID_RE = re.compile(r"\bpack-[a-z0-9][a-z0-9-]*-[0-9a-f]{10}\b")
_AGENT_PR_RE = re.compile(r"ai-code-auditor\b.*\b(findings?|packs?|nightly)\b", re.IGNORECASE | re.DOTALL)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value or "").strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def rel_path(repo: str, path: str) -> str:
    """Repo-relative path. Stored paths are absolute and differ by checkout (mirror or home)."""
    p = str(path or "")
    marker = f"/{repo}/"
    if marker in p:
        return p.rsplit(marker, 1)[1]
    return p.lstrip("./")


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


# ── packs ────────────────────────────────────────────────────────────────────

@dataclass
class PackView:
    id: str
    kind: str
    repo: str
    path: str
    line: int
    title: str
    status: str
    detected_at: datetime | None
    run_id: str
    history: list[dict[str, Any]] = field(default_factory=list)
    recurrence_of: str = ""
    duplicate_of: str = ""
    snippet: str = ""  # location.code_snippet: numbered lines ("  17| code") around the finding

    @property
    def is_finding(self) -> bool:
        return self.kind not in NON_FINDING_KINDS

    def last_resolved_at(self) -> datetime | None:
        for h in reversed(self.history):
            if h.get("status") == "resolved":
                return _parse_ts(h.get("at"))
        return None


def pack_view(data: dict[str, Any]) -> PackView:
    repo = str((data.get("repo") or {}).get("name") or "")
    loc = data.get("location") or {}
    prov = data.get("provenance") or {}
    gov = data.get("governance") or {}
    hist = data.get("status_history")
    return PackView(
        id=str(data.get("work_item_id") or ""),
        kind=str(data.get("kind") or "finding"),
        repo=repo,
        path=rel_path(repo, str(loc.get("file_path") or "")),
        line=int(loc.get("line") or 0),
        title=str((data.get("narrative") or {}).get("title") or ""),
        status=str(data.get("status") or "open"),
        detected_at=_parse_ts(prov.get("detected_at")),
        run_id=str(prov.get("run_id") or ""),
        history=[h for h in hist if isinstance(h, dict)] if isinstance(hist, list) else [],
        recurrence_of=str(gov.get("recurrence_of") or ""),
        duplicate_of=str(gov.get("duplicate_of") or ""),
        snippet=str(loc.get("code_snippet") or ""),
    )


# libyaml's loader is ~10x faster over ~2k pack files; same safe semantics.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_packs(output_dir: str | Path) -> list[PackView]:
    out: list[PackView] = []
    for path in sorted((Path(output_dir) / "agent_packs").glob("*.yaml")):
        try:
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=_YAML_LOADER)  # noqa: S506 — a safe loader
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("skip unreadable pack %s: %s", path.name, exc)
            continue
        if isinstance(data, dict) and data.get("work_item_id"):
            out.append(pack_view(data))
    return out


# ── scans ────────────────────────────────────────────────────────────────────

@dataclass
class Scan:
    """One repo as scanned by one run."""

    run_id: str
    repo: str
    at: datetime
    findings: list[dict[str, Any]]  # {path, line, title, type}


def lookback_days() -> int:
    try:
        return max(1, int(os.getenv("AI_AUDIT_TRUST_LOOKBACK_DAYS", "120")))
    except ValueError:
        return 120


def load_scans(session, *, since: datetime | None = None) -> list[Scan]:
    """Every (run, repo) scan with its findings, oldest first.

    A repo a run scanned with no findings is a scan with an empty list (from the run's
    ``scanned_repos``), but only for pipelines that store findings at all. Failed runs don't
    count: their silence proves nothing."""
    from sqlalchemy import func, select

    from auditor.db import AuditRun, FindingRecord, as_utc
    from auditor.run_views import SUCCESS_STATUSES

    since = since or datetime.now(UTC) - timedelta(days=lookback_days())
    runs = {r.run_id: r for r in session.execute(select(AuditRun).where(AuditRun.started_at >= since)).scalars().all()}
    first_seen = dict(session.execute(
        select(FindingRecord.run_id, func.min(FindingRecord.created_at))
        .where(FindingRecord.created_at >= since).group_by(FindingRecord.run_id)
    ).all())
    by_scan: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in session.execute(select(FindingRecord).where(FindingRecord.created_at >= since)).scalars().all():
        by_scan[(r.run_id, r.repo)].append({
            "path": rel_path(r.repo, r.file_path), "line": int(r.line or 0),
            "title": r.title or "", "type": r.finding_type or "",
        })
    # Ecosystem runs list scanned_repos but store no findings rows; only pipelines that do store
    # findings can report "scanned, nothing found".
    storing = {runs[rid].pipeline for rid, _repo in by_scan if rid in runs}
    for run_id, run in runs.items():
        if run.status not in SUCCESS_STATUSES or run.pipeline not in storing:
            continue
        for repo in (run.metadata_json or {}).get("scanned_repos") or []:
            by_scan.setdefault((run_id, str(repo)), [])
    scans: list[Scan] = []
    for (run_id, repo), findings in by_scan.items():
        run = runs.get(run_id)
        if run is not None and run.status not in SUCCESS_STATUSES:
            continue
        at = as_utc(run.started_at) if run is not None and run.started_at else as_utc(first_seen.get(run_id))
        if at is None:
            continue
        scans.append(Scan(run_id=run_id, repo=repo, at=at, findings=findings))
    scans.sort(key=lambda s: (s.at, s.run_id, s.repo))
    return scans


def _same_issue(a_title: str, a_line: int, b_title: str, b_line: int) -> bool:
    return a_title == b_title or reworded_score(a_title, b_title, a_line, b_line) > 0


# ── metrics ──────────────────────────────────────────────────────────────────

def resolution_events(packs: Iterable[PackView]) -> list[dict[str, Any]]:
    """Each transition into ``resolved``, with how it was justified.

    ``method`` is ``validator:<tool>`` when validate_patch closed it (tool from its note),
    otherwise ``manual``. Repeated ``resolved`` entries without a status in between are one
    resolution."""
    events = []
    for p in packs:
        prev = None
        for h in p.history:
            st = h.get("status")
            if st == "resolved" and prev != "resolved":
                note = str(h.get("note") or "")
                m = _TOOL_RE.search(note) if note.startswith("validate_patch passed") else None
                ev = _EVIDENCE_RE.search(note) if m else None
                events.append({
                    "pack_id": p.id, "repo": p.repo, "kind": p.kind, "at": h.get("at"),
                    "method": f"validator:{m.group(1)}" if m else "manual",
                    # evidence kind under the #58 bar; "" for resolutions from before it
                    "evidence": ev.group(1) if ev else "",
                })
            prev = st
    return events


def false_resolve_metric(events: list[dict[str, Any]]) -> dict[str, Any]:
    validator = [e for e in events if e["method"].startswith("validator:")]
    lint = [e for e in validator if e["method"] == "validator:lint_only"]
    by_repo: dict[str, dict[str, int]] = defaultdict(lambda: {"validator": 0, "lint_only": 0})
    for e in validator:
        by_repo[e["repo"]]["validator"] += 1
        by_repo[e["repo"]]["lint_only"] += e["method"] == "validator:lint_only"
    return {
        "resolutions": len(events),
        "validator": len(validator),
        "lint_only": len(lint),
        "manual": len(events) - len(validator),
        "methods": dict(Counter(e["method"] for e in events)),
        # resolutions under the evidence bar (#58), by evidence kind; a rising waiver share is a smell
        "by_evidence": dict(Counter(e["evidence"] for e in events if e["evidence"])),
        "lint_only_since_bar": sum(1 for e in events if e["evidence"] and e["method"] == "validator:lint_only"),
        "waiver_rate": _rate(sum(e["evidence"] == "waiver" for e in events),
                             sum(1 for e in events if e["evidence"])),
        "rate": _rate(len(lint), len(validator)),
        "by_repo": {r: {**c, "rate": _rate(c["lint_only"], c["validator"])} for r, c in sorted(by_repo.items())},
    }


def reopen_metric(packs: Iterable[PackView]) -> dict[str, Any]:
    ever, reopened = [], []
    for p in packs:
        seen_resolved = False
        for h in p.history:
            st = h.get("status")
            if st == "resolved":
                seen_resolved = True
            elif seen_resolved and st in REOPEN_STATUSES:
                reopened.append(p.id)
                break
        if seen_resolved:
            ever.append(p.id)
    return {"ever_resolved": len(ever), "reopened": len(reopened), "rate": _rate(len(reopened), len(ever)),
            "ids": reopened}


_DISPUTE_RE = re.compile(r"\b(already (fixed|handled|done)|not a bug|false positive|by design|no change needed|"
                         r"handled by|works as intended)\b", re.IGNORECASE)


def _match_score(a_title: str, a_line: int, b_title: str, b_line: int) -> float:
    return 1.0 if a_title == b_title else reworded_score(a_title, b_title, a_line, b_line)


def gc_metric(packs: Iterable[PackView]) -> dict[str, Any]:
    """Pack GC precision (#67): packs it closed as obsolete that a scan then reported again.
    That's the false-close rate: the GC said "gone", and it wasn't."""
    closed, back = [], []
    by_rule: Counter = Counter()
    back_by_rule: Counter = Counter()
    for p in packs:
        seen = False
        rule = ""
        for h in p.history:
            st = h.get("status")
            if st == "obsolete":
                if not seen:
                    closed.append(p.id)
                    m = re.match(r"gc \((\w+)\)", str(h.get("note") or ""))
                    rule = m.group(1) if m else "manual"
                    by_rule[rule] += 1
                seen = True
            elif seen and st in REOPEN_STATUSES:
                back.append(p.id)
                back_by_rule[rule] += 1
                break
    return {"closed": len(closed), "reopened": len(back), "rate": _rate(len(back), len(closed)),
            "by_rule": {r: {"closed": n, "reopened": back_by_rule[r], "rate": _rate(back_by_rule[r], n)}
                        for r, n in sorted(by_rule.items())},
            "reopened_ids": back[:50]}


def recurrence_metric(packs: list[PackView], scans: list[Scan]) -> dict[str, Any]:
    """Resolved finding packs whose issue came back after the resolution.

    Evidence is a later pack linked by ``recurrence_of`` (definitive), a later pack in the same
    file with the same or a reworded title, or a later scan reporting it. Each piece of
    evidence explains at most one resolution, best match first, so one new finding can't
    count as the return of several resolved packs. A resolution counts once a scan of its
    repo started after it ("observed"); before that it is pending.

    A recurrence is ``disputed`` when the pack was closed without a patch as already handled
    or not a bug (the agent and the scanner disagree), else ``regressed`` (a fix that didn't
    hold). ``rate`` counts regressions only."""
    linked: dict[str, list[PackView]] = defaultdict(list)
    by_file: dict[tuple[str, str], list[PackView]] = defaultdict(list)
    for p in packs:
        if p.recurrence_of:
            linked[p.recurrence_of].append(p)
        if p.is_finding and not p.duplicate_of:
            by_file[(p.repo, p.path)].append(p)
    scans_by_repo: dict[str, list[Scan]] = defaultdict(list)
    found_in: dict[tuple[str, str], list[tuple[Scan, dict[str, Any]]]] = defaultdict(list)
    for s in scans:
        scans_by_repo[s.repo].append(s)
        for f in s.findings:
            found_in[(s.repo, f["path"])].append((s, f))

    resolved: list[tuple[PackView, datetime]] = []
    pending = 0
    cands: list[tuple[float, int, tuple, str]] = []
    for p in packs:
        if not p.is_finding or p.status != "resolved" or not p.path:
            continue
        t = p.last_resolved_at()
        if t is None:
            continue
        i = len(resolved)
        resolved.append((p, t))
        for q in linked.get(p.id, []):
            if q.detected_at and q.detected_at > t:
                cands.append((2.0, i, ("pack", q.id), f"pack {q.id} (recurrence_of)"))
        for q in by_file.get((p.repo, p.path), []):
            if (q.id != p.id and q.detected_at and q.detected_at > t and q.run_id != p.run_id
                    and (sc := _match_score(p.title, p.line, q.title, q.line)) > 0):
                cands.append((sc, i, ("pack", q.id), f"pack {q.id}: {q.title[:80]}"))
        for s, f in found_in.get((p.repo, p.path), []):
            if s.at > t and (sc := _match_score(p.title, p.line, f["title"], f["line"])) > 0:
                # the same finding in several later scans is one piece of evidence
                cands.append((sc, i, ("scan", p.repo, f["path"], f["title"]), f"scan {s.run_id}: {f['title'][:80]}"))
    assigned: dict[int, str] = {}
    used: set[tuple] = set()
    for _sc, i, key, text in sorted(cands, key=lambda c: -c[0]):
        if i in assigned or key in used:
            continue
        assigned[i] = text
        used.add(key)

    observed = regressed = disputed = 0
    examples: list[dict[str, Any]] = []
    by_repo: dict[str, dict[str, int]] = defaultdict(lambda: {"observed": 0, "regressed": 0, "disputed": 0})
    for i, (p, t) in enumerate(resolved):
        evidence = assigned.get(i, "")
        if not evidence and not any(s.at > t for s in scans_by_repo.get(p.repo, [])):
            pending += 1
            continue
        observed += 1
        by_repo[p.repo]["observed"] += 1
        if not evidence:
            continue
        note = next((str(h.get("note") or "") for h in reversed(p.history) if h.get("status") == "resolved"), "")
        kind = "disputed" if (_DISPUTE_RE.search(note) and not note.startswith("validate_patch passed")) else "regressed"
        if kind == "disputed":
            disputed += 1
        else:
            regressed += 1
        by_repo[p.repo][kind] += 1
        if len(examples) < 50:
            examples.append({"pack_id": p.id, "repo": p.repo, "path": p.path, "title": p.title, "kind": kind,
                             "resolved_at": t.isoformat(), "resolve_note": note[:160], "evidence": evidence})
    return {
        "observed": observed, "regressed": regressed, "disputed": disputed, "pending": pending,
        "rate": _rate(regressed, observed),
        "dispute_rate": _rate(disputed, observed),
        "by_repo": {r: {**c, "rate": _rate(c["regressed"], c["observed"])} for r, c in sorted(by_repo.items())},
        "examples": examples,
    }


def _resolved_between(packs: list[PackView]) -> dict[tuple[str, str], list[tuple[datetime, PackView]]]:
    out: dict[tuple[str, str], list[tuple[datetime, PackView]]] = defaultdict(list)
    for p in packs:
        for h in p.history:
            if h.get("status") == "resolved" and (t := _parse_ts(h.get("at"))):
                out[(p.repo, p.path)].append((t, p))
    return out


def scan_persistence_metric(scans: list[Scan], packs: list[PackView]) -> dict[str, Any]:
    """How often consecutive scans of a repo report the same finding again."""
    resolved = _resolved_between(packs)
    by_repo: dict[str, list[Scan]] = defaultdict(list)
    for s in scans:
        by_repo[s.repo].append(s)
    total = refound = 0
    pairs = []
    per_repo: dict[str, dict[str, int]] = {}
    for repo, seq in sorted(by_repo.items()):
        r_total = r_refound = 0
        for prev, cur in pairwise(seq):
            if not prev.findings:
                continue
            p_total = p_refound = 0
            for f in prev.findings:
                fixed = any(prev.at < t <= cur.at and _same_issue(f["title"], f["line"], pk.title, pk.line)
                            for t, pk in resolved.get((repo, f["path"]), []))
                if fixed:
                    continue
                p_total += 1
                p_refound += any(g["path"] == f["path"] and _same_issue(f["title"], f["line"], g["title"], g["line"])
                                 for g in cur.findings)
            r_total += p_total
            r_refound += p_refound
            pairs.append({"repo": repo, "from": prev.run_id, "to": cur.run_id, "findings": p_total,
                          "refound": p_refound, "rate": _rate(p_refound, p_total)})
        if r_total:
            per_repo[repo] = {"findings": r_total, "refound": r_refound, "rate": _rate(r_refound, r_total)}
        total += r_total
        refound += r_refound
    return {"findings": total, "refound": refound, "rate": _rate(refound, total), "by_repo": per_repo,
            "pairs": pairs[-60:]}


def flow_metric(scans: list[Scan]) -> dict[str, Any]:
    """Findings new to / gone from each repo's scan, by ISO week and finding type."""
    weeks: dict[str, dict[str, Counter]] = defaultdict(lambda: {"new": Counter(), "gone": Counter()})
    by_repo: dict[str, list[Scan]] = defaultdict(list)
    for s in scans:
        by_repo[s.repo].append(s)
    for seq in by_repo.values():
        for prev, cur in pairwise(seq):
            y, w, _ = cur.at.isocalendar()
            wk = f"{y}-W{w:02d}"
            new, gone = _pair_delta(prev.findings, cur.findings)
            for f in new:
                weeks[wk]["new"][f["type"] or "unknown"] += 1
            for f in gone:
                weeks[wk]["gone"][f["type"] or "unknown"] += 1
    return {wk: {"new": dict(c["new"]), "gone": dict(c["gone"])} for wk, c in sorted(weeks.items())}


def _pair_delta(prev: list[dict[str, Any]], cur: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """(new in cur, gone from prev), exact keys first then rewordings in the same file, greedily."""
    key = lambda f: (f["path"], f["type"], f["title"])
    pk, ck = Counter(map(key, prev)), Counter(map(key, cur))
    new, gone = [], []
    seen: Counter = Counter()
    for f in cur:
        seen[key(f)] += 1
        if seen[key(f)] > pk.get(key(f), 0):
            new.append(f)
    seen = Counter()
    for f in prev:
        seen[key(f)] += 1
        if seen[key(f)] > ck.get(key(f), 0):
            gone.append(f)
    cands = sorted(((reworded_score(n["title"], g["title"], n["line"] + 1, g["line"] + 1), i, j)
                    for i, n in enumerate(new) for j, g in enumerate(gone)
                    if (n["path"], n["type"]) == (g["path"], g["type"])), reverse=True)
    used_n, used_g = set(), set()
    for score, i, j in cands:
        if score > 0 and i not in used_n and j not in used_g:
            used_n.add(i)
            used_g.add(j)
    return ([f for i, f in enumerate(new) if i not in used_n], [f for j, f in enumerate(gone) if j not in used_g])


# ── agent PRs ────────────────────────────────────────────────────────────────

def is_agent_pr(title: str, body: str) -> bool:
    """A merged PR that fixes audit packs: its body names a pack id, or its title is a fix of
    ai-code-auditor findings/packs ("fix: resolve 21 ai-code-auditor findings"). Docs PRs
    about the pack flow and release syncs that merely mention it are not fixes."""
    t = title.strip()
    if re.match(r"(release|docs|chore)\b", t, re.IGNORECASE):
        return False
    if PACK_ID_RE.search(body or ""):
        return True
    return bool(re.match(r"(fix|resolve)", t, re.IGNORECASE) and _AGENT_PR_RE.search(t))


GitLog = Callable[[str, str], list[tuple[str, str, str]]]  # (repo, since_iso) -> [(sha, iso_date, message)]


def mirror_git_log(mirror_paths: dict[str, Path]) -> GitLog:
    """Commits on the mirror's ``origin/dev`` (else HEAD) since a date: (sha, date, full message)."""
    def log(repo: str, since: str) -> list[tuple[str, str, str]]:
        path = mirror_paths.get(repo)
        if not path or not (path / ".git").exists():
            raise FileNotFoundError(f"no mirror for {repo}")
        for ref in ("origin/dev", "HEAD"):
            r = subprocess.run(["git", "log", ref, f"--since={since}", "--format=%H%x1f%cI%x1f%B%x1e"],
                               cwd=path, capture_output=True, text=True, timeout=30, check=False)
            if r.returncode == 0:
                break
        else:
            raise RuntimeError(r.stderr.strip()[:200])
        out = []
        for rec in r.stdout.split("\x1e"):
            parts = rec.strip("\n").split("\x1f")
            if len(parts) == 3:
                out.append((parts[0], parts[1], parts[2]))
        return out
    return log


def classify_followups(pr: dict[str, Any], commits: list[tuple[str, str, str]], *,
                       window_days: int = REVERT_WINDOW_DAYS) -> dict[str, list[str]]:
    """Reverts and fix-forwards of *pr* among *commits*, within the window after its merge."""
    merged = _parse_ts(pr.get("merged_at"))
    if merged is None:
        return {"reverts": [], "fixes": []}
    end = merged + timedelta(days=window_days)
    pr_shas = {s for s in [pr.get("merge_sha"), *(pr.get("commit_shas") or [])] if s}
    num = int(pr.get("number") or 0)
    slug = str(pr.get("slug") or "")
    ref_re = re.compile(rf"(?<![\w/])(?:{re.escape(slug)})?#{num}\b") if num else None
    title = str(pr.get("title") or "")
    reverts, fixes = [], []
    for sha, date, msg in commits:
        when = _parse_ts(date)
        if sha in pr_shas or when is None or not (merged < when <= end):
            continue
        first = msg.strip().splitlines()[0] if msg.strip() else ""
        named = bool(ref_re and ref_re.search(msg))
        reverted_shas = set(re.findall(r"This reverts commit ([0-9a-f]{7,40})", msg))
        if (any(any(p.startswith(r) for p in pr_shas) for r in reverted_shas)
                or (title and first == f'Revert "{title}"')
                or (named and first.lower().startswith("revert"))):
            reverts.append(sha)
        elif named and re.match(r"(fix|hotfix|bugfix)\b", first, re.IGNORECASE):
            fixes.append(sha)
    return {"reverts": reverts, "fixes": fixes}


def agent_pr_metric(prs: list[dict[str, Any]], git_log: GitLog | None, *, now: datetime,
                    window_days: int = REVERT_WINDOW_DAYS) -> dict[str, Any]:
    rows = []
    for pr in prs:
        row = {k: pr.get(k) for k in ("repo", "number", "title", "url", "merged_at")}
        merged = _parse_ts(pr.get("merged_at"))
        row["mature"] = bool(merged and now - merged >= timedelta(days=window_days))
        try:
            commits = git_log(pr["repo"], (merged - timedelta(days=1)).isoformat()) if git_log and merged else None
        except Exception as exc:  # noqa: BLE001 — a missing mirror leaves the PR unobserved
            logger.info("no follow-up data for %s#%s: %s", pr.get("repo"), pr.get("number"), exc)
            commits = None
        if commits is None:
            row["observed"] = False
        else:
            row["observed"] = True
            row.update(classify_followups(pr, commits, window_days=window_days))
        rows.append(row)
    obs = [r for r in rows if r["observed"]]
    mature = [r for r in obs if r["mature"]]
    rev = [r for r in obs if r.get("reverts")]
    fix = [r for r in obs if r.get("fixes") and not r.get("reverts")]
    return {
        "merged": len(rows), "observed": len(obs), "mature": len(mature),
        "reverted": len(rev), "fixed_forward": len(fix),
        "rate": _rate(len(rev), len(obs)),
        "mature_rate": _rate(sum(1 for r in mature if r.get("reverts")), len(mature)),
        "fix_forward_rate": _rate(len(fix), len(obs)),
        "window_days": window_days,
        "prs": sorted(rows, key=lambda r: str(r.get("merged_at") or ""), reverse=True)[:100],
    }


_PR_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number title body url mergedAt
      repository { name nameWithOwner }
      mergeCommit { oid }
      commits(first: 100) { nodes { commit { oid } } }
    } }
  }
}"""


def fetch_agent_prs(graphql: Callable[[str, dict[str, Any]], dict[str, Any]], owner: str, *,
                    repos: set[str] | None = None, max_pages: int = 10) -> list[dict[str, Any]]:
    """Merged PRs under *owner* that fix audit packs (see :func:`is_agent_pr`)."""
    found: dict[str, dict[str, Any]] = {}
    for q in (f'user:{owner} is:pr is:merged "ai-code-auditor" in:title,body',
              f'user:{owner} is:pr is:merged "pack-" in:body'):
        cursor = None
        for _ in range(max_pages):
            data = graphql(_PR_QUERY, {"q": q, "cursor": cursor}).get("search") or {}
            for n in data.get("nodes") or []:
                if not n or not n.get("mergedAt"):
                    continue
                repo = (n.get("repository") or {}).get("name") or ""
                if repos is not None and repo not in repos:
                    continue
                if not is_agent_pr(n.get("title") or "", n.get("body") or ""):
                    continue
                found[n["url"]] = {
                    "repo": repo, "slug": (n.get("repository") or {}).get("nameWithOwner") or "",
                    "number": n.get("number"), "title": n.get("title") or "", "url": n["url"],
                    "merged_at": n["mergedAt"], "merge_sha": (n.get("mergeCommit") or {}).get("oid") or "",
                    "commit_shas": [c["commit"]["oid"] for c in ((n.get("commits") or {}).get("nodes") or [])
                                    if c and c.get("commit")],
                    "pack_ids": sorted(set(PACK_ID_RE.findall(n.get("body") or ""))),
                }
            info = data.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
    return sorted(found.values(), key=lambda p: p["merged_at"])


# ── report ───────────────────────────────────────────────────────────────────

def compute(packs: list[PackView], scans: list[Scan], prs: list[dict[str, Any]] | None,
            git_log: GitLog | None, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    events = resolution_events(packs)
    report = {
        "generated_at": now.isoformat(),
        "false_resolve": false_resolve_metric(events),
        "reopen": reopen_metric(packs),
        "recurrence": recurrence_metric(packs, scans),
        "scan_persistence": scan_persistence_metric(scans, packs),
        "gc": gc_metric(packs),
        "agent_prs": (agent_pr_metric([p for p in prs if is_agent_pr(p.get("title") or "", " ".join(p.get("pack_ids") or []))],
                                      git_log, now=now) if prs is not None else None),
        "flow": flow_metric(scans),
        "inputs": {
            "packs": len(packs), "finding_packs": sum(p.is_finding for p in packs),
            "status_counts": dict(Counter(p.status for p in packs)),
            "scans": len(scans), "runs": len({s.run_id for s in scans}),
            "first_scan": scans[0].at.isoformat() if scans else None,
            "prs_cached": prs is not None,
        },
    }
    report["headline"] = headline(report)
    return report


def headline(report: dict[str, Any]) -> dict[str, Any]:
    prs = report.get("agent_prs") or {}
    return {
        "at": report["generated_at"],
        "false_resolve_rate": report["false_resolve"]["rate"],
        "reopen_rate": report["reopen"]["rate"],
        "recurrence_rate": report["recurrence"]["rate"],
        "recurrence_observed": report["recurrence"]["observed"],
        "dispute_rate": report["recurrence"]["dispute_rate"],
        "waiver_rate": report["false_resolve"]["waiver_rate"],
        "gc_closed": report["gc"]["closed"],
        "gc_reopen_rate": report["gc"]["rate"],
        "unverified_packs": report["inputs"]["status_counts"].get("unverified", 0),
        "scan_persistence": report["scan_persistence"]["rate"],
        "revert_rate": prs.get("rate"),
        "agent_prs": prs.get("merged"),
        "open_finding_packs": sum(1 for _ in ()),  # filled below
    }


def _mirror_paths(mirrors_path: str | Path) -> dict[str, Path]:
    try:
        from conductor.mirrors import load_mirrors_config

        cfg = load_mirrors_config(mirrors_path)
    except Exception as exc:  # noqa: BLE001
        logger.info("no mirrors config: %s", exc)
        return {}
    return {str(m["name"]): Path(m["path"]) for m in cfg.get("mirrors") or [] if m.get("name") and m.get("path")}


def load_prs_cache(output_dir: str | Path) -> list[dict[str, Any]] | None:
    try:
        data = json.loads((Path(output_dir) / PRS_CACHE_NAME).read_text())
    except (OSError, ValueError):
        return None
    return data.get("prs") if isinstance(data, dict) else None


def refresh_prs(output_dir: str | Path, *, owner: str = "smk762", token: str = "",
                mirrors_path: str | Path = "config/repo_mirrors.yaml") -> list[dict[str, Any]]:
    from auditor.merge_poll import github_token
    from auditor.pr_intel import github_graphql

    tok = token or github_token()
    if not tok:
        raise RuntimeError("GITHUB_TOKEN (or GH_TOKEN) is required to list agent PRs")
    repos = set(_mirror_paths(mirrors_path)) or None
    prs = fetch_agent_prs(github_graphql(tok), owner, repos=repos)
    path = Path(output_dir) / PRS_CACHE_NAME
    path.write_text(json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "prs": prs}, indent=1))
    return prs


def build(output_dir: str | Path, *, session=None, refresh_github: bool = False,
          mirrors_path: str | Path = "config/repo_mirrors.yaml", now: datetime | None = None) -> dict[str, Any]:
    out = Path(output_dir)
    packs = load_packs(out)
    if session is None:
        from auditor import ui

        with ui._db_session() as s:
            scans = load_scans(s)
    else:
        scans = load_scans(session)
    prs = load_prs_cache(out)
    if refresh_github:
        try:
            prs = refresh_prs(out, mirrors_path=mirrors_path)
        except Exception as exc:  # noqa: BLE001 — keep the cached list, say why
            logger.warning("agent PR refresh failed, using cache: %s", exc)
    git_log = mirror_git_log(_mirror_paths(mirrors_path))
    report = compute(packs, scans, prs, git_log, now=now)
    report["headline"]["open_finding_packs"] = sum(1 for p in packs if p.is_finding and p.status == "open"
                                                   and not p.duplicate_of)
    return report


def refresh(output_dir: str | Path | None = None, **kw: Any) -> dict[str, Any]:
    """Compute, write ``trust.json`` and append the headline to ``trust_history.jsonl``."""
    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    report = build(out, **kw)
    (out / REPORT_NAME).write_text(json.dumps(report, indent=1, default=str))
    with (out / HISTORY_NAME).open("a") as fh:
        fh.write(json.dumps(report["headline"], default=str) + "\n")
    return report


def trust_metrics_enabled() -> bool:
    return os.getenv("AI_AUDIT_TRUST_METRICS", "1").strip().lower() not in {"0", "false", "no", "off"}


def refresh_quietly(output_dir: str | Path | None = None, **kw: Any) -> dict[str, Any]:
    """Pipeline tail: never fail the caller over the metric. Refreshes agent PRs when a GitHub
    token is set (otherwise the cached list is used)."""
    if not trust_metrics_enabled():
        return {"skipped": "AI_AUDIT_TRUST_METRICS=0"}
    try:
        from auditor.merge_poll import github_token

        kw.setdefault("refresh_github", bool(github_token()))
        return refresh(output_dir, **kw)["headline"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("trust metrics refresh failed: %s", exc)
        return {"error": str(exc)[:300]}


def load_report(output_dir: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(output_dir) / REPORT_NAME).read_text())
    except (OSError, ValueError):
        return None


def load_history(output_dir: str | Path, *, limit: int = 365) -> list[dict[str, Any]]:
    try:
        lines = (Path(output_dir) / HISTORY_NAME).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
