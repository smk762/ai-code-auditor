"""Blocked-PR intel: what is stuck across the ecosystem, why, and the order to clear it.

Read-only. One GitHub GraphQL query per repo (mirrors in ``config/repo_mirrors.yaml``),
then a deterministic classifier and planner:

* **blockers** per PR: failing CI, merge conflict, branch behind, changes requested,
  review required, wrong base, stacked on another PR, depends on another PR/issue
  (parsed from the body: "Depends on …", "Stacked on #N", "Merge after …"), dependency
  closed without merging, draft.
* **graph**: which PRs each PR holds up, across repos.
* **plan**: the suggested resolution path — dependencies first, then whatever unblocks
  the most, release PRs before the rest, oldest first — as ordered steps.
* **linchpins** (``auditor/linchpin.py``): open PRs *and issues* that hold up work in
  another repo, or sit in a dependency cycle, marked P0 and ranked first.
* **spoken**: a TTS-safe sentence or three for the morning briefing.

Nothing here comments on, pushes to or merges a PR. The one write is opt-in: with
``--label-p0`` (or ``AI_AUDIT_LINCHPIN_LABELS=1``) linchpins get a ``P0`` label.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from auditor import linchpin
from auditor.pr_model import (
    WAITING,
    Blocker,
    PrIntel,
    days_since,
    parse_dependants,
    parse_dependencies,
    slug_ref,
)

logger = logging.getLogger(__name__)

REPORT_NAME = "pr_intel.json"
HTML_PATH = Path(__file__).parent / "static" / "pr_dashboard.html"
GRAPHQL_URL = "https://api.github.com/graphql"
STALE_DAYS = 14

# Order matters for the per-PR "next action": the first actionable blocker wins.
BLOCKER_ORDER = (
    "dep_closed", "depends_on", "stacked", "wrong_base", "conflict", "ci_failing",
    "behind", "changes_requested", "review_required", "checks_pending", "draft",
)
# Blockers the PR's author can act on right now (vs waiting on something else).
ACTIONABLE = {"dep_closed", "wrong_base", "conflict", "ci_failing", "behind", "changes_requested"}

_PR_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequests(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url isDraft createdAt updatedAt baseRefName headRefName isCrossRepository body
        mergeable mergeStateStatus reviewDecision
        author { login }
        labels(first: 10) { nodes { name } }
        closingIssuesReferences(first: 10) { nodes { number repository { nameWithOwner } } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state
          contexts(first: 50) { nodes {
            __typename
            ... on CheckRun { name conclusion status detailsUrl }
            ... on StatusContext { context state targetUrl }
          } }
        } } } }
      }
    }
    issues(states: OPEN, first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url body createdAt updatedAt
        labels(first: 10) { nodes { name } }
        blockedBy(first: 20) { nodes { number title url state repository { nameWithOwner } } }
        blocking(first: 20) { nodes { number title url state repository { nameWithOwner } } }
      }
    }
  }
}
"""

_REF_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issueOrPullRequest(number: $number) {
      __typename
      ... on PullRequest { state title url }
      ... on Issue { state title url }
    }
  }
}
"""

# ── GitHub access ─────────────────────────────────────────────────────────────

GraphQL = Callable[[str, dict[str, Any]], dict[str, Any]]


def github_graphql(token: str, *, timeout_s: float = 30.0) -> GraphQL:
    def call(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            GRAPHQL_URL,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "ai-code-auditor-pr-intel",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode())
        if payload.get("errors") and not payload.get("data"):
            raise RuntimeError(str(payload["errors"])[:400])
        return payload.get("data") or {}

    return call


def _checks(node: dict[str, Any]) -> tuple[list[dict[str, str]], int]:
    failed: list[dict[str, str]] = []
    pending = 0
    commits = ((node.get("commits") or {}).get("nodes") or [])
    rollup = ((commits[0].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
    for c in ((rollup.get("contexts") or {}).get("nodes") or []):
        if c.get("__typename") == "CheckRun":
            concl = (c.get("conclusion") or "").upper()
            if concl in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
                failed.append({"name": c.get("name") or "check", "url": c.get("detailsUrl") or ""})
            elif (c.get("status") or "").upper() != "COMPLETED":
                pending += 1
        else:
            st = (c.get("state") or "").upper()
            if st in ("FAILURE", "ERROR"):
                failed.append({"name": c.get("context") or "status", "url": c.get("targetUrl") or ""})
            elif st in ("PENDING", "EXPECTED"):
                pending += 1
    return failed, pending


def _is_release(title: str, head: str, base: str) -> bool:
    return bool(re.match(r"\s*(?:release\b|chore\(release\))", title, re.I)) or (
        base in ("main", "master") and head in ("dev", "develop")
    )


def _node_to_intel(repo: str, slug: str, default_branch: str, n: dict[str, Any]) -> PrIntel:
    failed, pending = _checks(n)
    owner, name = slug.split("/", 1)
    title = str(n.get("title") or "")
    body = str(n.get("body") or "")
    head, base = str(n.get("headRefName") or ""), str(n.get("baseRefName") or "")
    return PrIntel(
        repo=repo,
        slug=slug,
        number=int(n.get("number") or 0),
        title=title,
        url=str(n.get("url") or ""),
        author=str((n.get("author") or {}).get("login") or ""),
        base=base,
        head=head,
        default_branch=default_branch,
        draft=bool(n.get("isDraft")),
        created_at=str(n.get("createdAt") or ""),
        updated_at=str(n.get("updatedAt") or ""),
        mergeable=str(n.get("mergeable") or "UNKNOWN"),
        merge_state=str(n.get("mergeStateStatus") or "UNKNOWN"),
        review_decision=str(n.get("reviewDecision") or ""),
        failed_checks=failed,
        pending_checks=pending,
        labels=[str(x.get("name")) for x in ((n.get("labels") or {}).get("nodes") or [])],
        release=_is_release(title, head, base),
        deps=parse_dependencies(body, owner=owner, repo=name),
        dependants=parse_dependants(body, owner=owner, repo=name),
        closes=[
            {"slug": str((c.get("repository") or {}).get("nameWithOwner") or slug), "number": int(c.get("number") or 0)}
            for c in ((n.get("closingIssuesReferences") or {}).get("nodes") or [])
        ],
        cross_repo=bool(n.get("isCrossRepository")),
    )


def fetch_repo(
    gql: GraphQL, repo: str, slug: str, *, retries: int = 2, retry_wait_s: float = 3.0
) -> tuple[list[PrIntel], list[Any]]:
    """Open PRs and open issues for one repo. GitHub computes ``mergeable`` lazily and
    answers UNKNOWN on the first ask, so re-query while any PR is still UNKNOWN."""
    owner, name = slug.split("/", 1)
    prs: list[PrIntel] = []
    repo_data: dict[str, Any] = {}
    for attempt in range(retries + 1):
        data = gql(_PR_QUERY, {"owner": owner, "name": name})
        repo_data = data.get("repository") or {}
        default_branch = str((repo_data.get("defaultBranchRef") or {}).get("name") or "dev")
        nodes = ((repo_data.get("pullRequests") or {}).get("nodes") or [])
        prs = [_node_to_intel(repo, slug, default_branch, n) for n in nodes]
        if attempt == retries or not any(p.mergeable == "UNKNOWN" for p in prs):
            break
        time.sleep(retry_wait_s)
    issues = [linchpin.issue_from_node(repo, slug, n) for n in ((repo_data.get("issues") or {}).get("nodes") or [])]
    return prs, issues


def fetch_repo_prs(
    gql: GraphQL, repo: str, slug: str, *, retries: int = 2, retry_wait_s: float = 3.0
) -> list[PrIntel]:
    return fetch_repo(gql, repo, slug, retries=retries, retry_wait_s=retry_wait_s)[0]


# ── classification ────────────────────────────────────────────────────────────


def classify(
    prs: list[PrIntel],
    *,
    resolve_ref: Callable[[str, int], dict[str, Any] | None] | None = None,
    now: datetime | None = None,
) -> None:
    """Fill ``blockers``, ``state``, ``blocks``, ``next_action`` and ``score`` in place.

    *resolve_ref(slug, number)* looks up a dependency that is not among the open PRs; it
    returns ``{"state", "title", "url"}`` or None. Without it, such refs are ignored.
    """
    now = now or datetime.now(timezone.utc)
    by_ref = {(p.slug.lower(), p.number): p for p in prs}
    # Only same-repo heads can be another PR's base: a fork's "main" or "patch-1" is not.
    by_head = {(p.slug.lower(), p.head): p for p in prs if not p.cross_repo}

    for p in prs:
        p.release = p.release or _is_release(p.title, p.head, p.base)
        b: list[Blocker] = []
        waits_on: set[tuple[str, int]] = set()
        # Stacked: the base branch is another open PR's head.
        parent = by_head.get((p.slug.lower(), p.base))
        if parent is not None and parent is not p:
            waits_on.add((parent.slug.lower(), parent.number))
            b.append(Blocker("stacked", f"stacked on {parent.ref}; merges after it", parent.ref, parent.url))
        elif p.base not in (p.default_branch, "main", "master") and parent is None:
            b.append(Blocker("wrong_base", f"targets '{p.base}', which is not an open PR branch"))
        elif p.base in ("main", "master") and p.default_branch not in ("main", "master") and not p.release:
            b.append(Blocker("wrong_base", f"targets {p.base}; PRs go to {p.default_branch}"))
        for dep in p.deps:
            key = (dep["slug"].lower(), int(dep["number"]))
            if key in waits_on or key == (p.slug.lower(), p.number):
                continue
            ref = slug_ref(dep["slug"], dep["number"])
            open_pr = by_ref.get(key)
            if open_pr is not None:
                waits_on.add(key)
                kind = "stacked" if dep["verb"] == "stacked" else "depends_on"
                b.append(Blocker(kind, f"waits on {open_pr.ref} ({open_pr.title[:60]})", open_pr.ref, open_pr.url))
                continue
            info = resolve_ref(dep["slug"], int(dep["number"])) if resolve_ref else None
            if not info:
                continue
            state = str(info.get("state") or "").upper()
            if state == "OPEN":
                waits_on.add(key)
                b.append(Blocker("depends_on", f"waits on {ref} ({str(info.get('title') or '')[:60]})", ref, str(info.get("url") or "")))
            elif state == "CLOSED" and dep["verb"] == "stacked":
                b.append(Blocker("dep_closed", f"{ref} was closed without merging; retarget to {p.default_branch}", ref, str(info.get("url") or "")))
        if p.mergeable == "CONFLICTING" or p.merge_state == "DIRTY":
            b.append(Blocker("conflict", f"conflicts with {p.base}"))
        if p.failed_checks:
            names = ", ".join(c["name"] for c in p.failed_checks[:4])
            b.append(Blocker("ci_failing", f"failing: {names}", url=p.failed_checks[0].get("url", "")))
        if p.merge_state == "BEHIND":
            b.append(Blocker("behind", f"behind {p.base}"))
        if p.review_decision == "CHANGES_REQUESTED":
            b.append(Blocker("changes_requested", "changes requested"))
        elif p.review_decision == "REVIEW_REQUIRED":
            b.append(Blocker("review_required", "needs an approving review"))
        if p.pending_checks and not p.failed_checks:
            b.append(Blocker("checks_pending", f"{p.pending_checks} check(s) still running"))
        if p.draft:
            b.append(Blocker("draft", "draft"))
        b.sort(key=lambda x: BLOCKER_ORDER.index(x.kind))
        p.blockers = b
        p.state = _state(p)
        p.next_action = _next_action(p)

    # Reverse edges, transitively: what does each PR hold up?
    direct: dict[str, set[str]] = {}
    for p in prs:
        for bl in p.blockers:
            if bl.kind in WAITING and bl.ref:
                direct.setdefault(bl.ref, set()).add(p.ref)
    for p in prs:
        seen: set[str] = set()
        stack = list(direct.get(p.ref, ()))
        while stack:
            r = stack.pop()
            if r in seen or r == p.ref:
                continue
            seen.add(r)
            stack.extend(direct.get(r, ()))
        p.blocks = sorted(seen)
        p.score = _score(p, now)


def _state(p: PrIntel) -> str:
    kinds = {b.kind for b in p.blockers}
    if p.draft:
        return "draft"
    if kinds & ACTIONABLE:
        return "blocked"
    if kinds & WAITING:
        return "waiting"
    if "checks_pending" in kinds or p.mergeable == "UNKNOWN":
        return "pending"
    return "ready"  # review_required alone: you are the reviewer — merge is the action


def _next_action(p: PrIntel) -> str:
    kinds = [b.kind for b in p.blockers]
    first = {b.kind: b for b in reversed(p.blockers)}
    if "dep_closed" in first:
        return f"Retarget to {p.default_branch} (stack parent {first['dep_closed'].ref} was closed)"
    if "wrong_base" in first:
        return f"Retarget to {p.default_branch}"
    waits = [b.ref for b in p.blockers if b.kind in WAITING and b.ref]
    if "conflict" in first:
        if waits:
            return f"Resolve conflicts after {', '.join(waits)} merges (they may clear)"
        return f"Merge {p.base} into {p.head} and resolve conflicts"
    if "ci_failing" in first:
        return f"Fix failing CI ({first['ci_failing'].detail.removeprefix('failing: ')})"
    if waits:
        return f"Wait for {', '.join(waits)}; then merge"
    if "behind" in first:
        return f"Update {p.head} from {p.base}"
    if "changes_requested" in first:
        return "Address review comments"
    if p.draft:
        return "Finish and mark ready for review"
    if "checks_pending" in kinds:
        return "Wait for CI, then merge"
    if p.mergeable == "UNKNOWN":
        return "Recheck (GitHub has not computed mergeability)"
    if "review_required" in first:
        return "Review and merge"
    return "Merge"


def _score(p: PrIntel, now: datetime) -> float:
    """Higher = deal with it sooner."""
    s = 1.0 + 2.0 * len(p.blocks)
    if p.release:
        s += 1.5
    kinds = {b.kind for b in p.blockers}
    if kinds & ACTIONABLE:
        s += 1.0
    if kinds & WAITING:
        s -= 0.5  # its own turn comes after what it waits on
    s += min(p.age_days(now) / STALE_DAYS, 1.0)
    if p.draft:
        s *= 0.3
    return round(s, 2)


# ── resolution plan ───────────────────────────────────────────────────────────


def resolution_plan(prs: list[PrIntel]) -> list[dict[str, Any]]:
    """Ordered steps that clear the backlog: prerequisites before dependants, and within
    that, highest score first. Drafts are left out (they are parked, not stuck)."""
    live = [p for p in prs if not p.draft]
    by_ref = {p.ref: p for p in live}
    prereq: dict[str, set[str]] = {
        p.ref: {b.ref for b in p.blockers if b.kind in WAITING and b.ref in by_ref} for p in live
    }
    ext_waits = {
        p.ref: [b.ref for b in p.blockers if b.kind in WAITING and b.ref and b.ref not in by_ref]
        for p in live
    }
    done: set[str] = set()
    steps: list[dict[str, Any]] = []
    remaining = sorted(live, key=lambda p: (-p.score, p.ref))
    while remaining:
        ready = [p for p in remaining if prereq[p.ref] <= done]
        cycle_break = not ready
        if cycle_break:  # dependency cycle: break it at the highest score
            ready = remaining[:1]
        p = ready[0]
        remaining.remove(p)
        done.add(p.ref)
        action = p.next_action
        # "Wait for X; then merge" becomes "Merge" only when every wait is an earlier step:
        # not on a cycle break, not with an external wait, not with a wait that names no ref.
        waits_covered = (
            not cycle_break
            and not ext_waits[p.ref]
            and not any(b.kind in WAITING and not b.ref for b in p.blockers)
        )
        if cycle_break:
            action = f"Break cycle: {action}"
        elif prereq[p.ref] and action.startswith("Wait for") and waits_covered:
            action = "Merge" if not ({b.kind for b in p.blockers} & ACTIONABLE) else action
        steps.append(
            {
                "step": len(steps) + 1,
                "ref": p.ref,
                "url": p.url,
                "title": p.title,
                "state": p.state,
                "action": action,
                "after": sorted(prereq[p.ref]) + ext_waits[p.ref],
                "unblocks": p.blocks,
                "release": p.release,
                "p0": p.p0,
                "score": p.score,
            }
        )
    return steps


# ── spoken summary ────────────────────────────────────────────────────────────

_SPOKEN_ACTION = {
    "conflict": "resolve merge conflicts",
    "ci_failing": "fix failing CI",
    "behind": "update the branch",
    "changes_requested": "address review comments",
    "wrong_base": "retarget it to dev",
    "dep_closed": "retarget it, its parent was closed",
}


def spoken_summary(report: dict[str, Any], *, max_items: int = 2) -> str:
    """Two or three TTS-safe sentences: counts, then the top actionable blockers."""
    c = report.get("counts") or {}
    total = int(c.get("open") or 0)
    if not total and not report.get("linchpins"):
        return ""
    blocked = int(c.get("blocked") or 0)
    waiting = int(c.get("waiting") or 0)
    ready = int(c.get("ready") or 0)
    parts = [
        f"Pull requests: {total} open, {blocked} blocked, {waiting} waiting on another, "
        f"{ready} ready to merge."
    ]
    lp = linchpin.spoken_linchpins(report)
    if lp:
        parts.append(lp)
    top = [p for p in report.get("prs") or [] if p.get("state") == "blocked"][:max_items]
    bits = []
    for p in top:
        kinds = [b["kind"] for b in p.get("blockers") or [] if b["kind"] in _SPOKEN_ACTION]
        what = _SPOKEN_ACTION.get(kinds[0], "unblock it") if kinds else "unblock it"
        why = ""
        if p.get("release"):
            why = ", it holds a release"
        elif p.get("blocks"):
            n = len(p["blocks"])
            why = f", it holds up {n} other{'s' if n != 1 else ''}"
        bits.append(f"{p['ref']}: {what}{why}")
    if bits:
        parts.append("Start with " + "; then ".join(bits) + ".")
    elif ready:
        first = next((s for s in report.get("plan") or [] if s.get("state") == "ready"), None)
        if first:
            parts.append(f"Nothing is stuck; merge {first['ref']} first.")
    return " ".join(parts)


# ── top level ─────────────────────────────────────────────────────────────────


def build_report(
    targets: list[tuple[str, str]],
    gql: GraphQL,
    *,
    now: datetime | None = None,
    retry_wait_s: float = 3.0,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)

    prs: list[PrIntel] = []
    issues: list[Any] = []
    errors: list[dict[str, str]] = []
    for name, slug in targets:
        try:
            got_prs, got_issues = fetch_repo(gql, name, slug, retry_wait_s=retry_wait_s)
            prs.extend(got_prs)
            issues.extend(got_issues)
        except (urllib.error.URLError, RuntimeError, ValueError, OSError) as exc:
            logger.warning("pr intel failed for %s: %s", slug, exc)
            errors.append({"repo": name, "error": str(exc)[:300]})

    cache: dict[tuple[str, int], dict[str, Any] | None] = {}

    def resolve(slug: str, number: int) -> dict[str, Any] | None:
        key = (slug.lower(), number)
        if key not in cache:
            owner, name = slug.split("/", 1)
            try:
                data = gql(_REF_QUERY, {"owner": owner, "name": name, "number": number})
                cache[key] = (data.get("repository") or {}).get("issueOrPullRequest")
            except (urllib.error.URLError, RuntimeError, ValueError, OSError) as exc:
                logger.info("could not resolve %s#%d: %s", slug, number, exc)
                cache[key] = None
        return cache[key]

    classify(prs, resolve_ref=resolve, now=now)
    scan = linchpin.analyse(prs, issues, resolve_ref=resolve, mirrored={s.lower() for _, s in targets}, now=now)
    state_rank = {"blocked": 0, "waiting": 1, "pending": 2, "ready": 3, "draft": 4}
    prs.sort(key=lambda p: (state_rank.get(p.state, 9), -p.score, p.ref))
    counts = {"open": len(prs)}
    for s in state_rank:
        counts[s] = sum(1 for p in prs if p.state == s)
    counts["p0"] = len(scan["linchpins"])
    counts["cycles"] = len(scan["cycles"])
    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "repos_checked": len(targets),
        "counts": counts,
        "stale_days": STALE_DAYS,
        "stale": sum(1 for p in prs if p.age_days(now) >= STALE_DAYS),
        "prs": [p.to_dict(now) for p in prs],
        "plan": resolution_plan(prs),
        "linchpins": scan["linchpins"],
        "cycles": scan["cycles"],
        "issues": scan["issues"],
        "edges": scan["edges"],
        "errors": errors,
    }
    report["spoken"] = spoken_summary(report)
    return report


def report_path(output_dir: str | Path | None = None) -> Path:
    return Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports") / REPORT_NAME


def refresh(
    *,
    output_dir: str | Path | None = None,
    mirrors_path: str | Path = "config/repo_mirrors.yaml",
    token: str | None = None,
    label: bool | None = None,
    label_dry_run: bool = False,
) -> dict[str, Any]:
    """Build the report from GitHub and write ``reports/pr_intel.json`` atomically.

    *label* (default ``AI_AUDIT_LINCHPIN_LABELS``) puts the P0 label on linchpins in
    mirrored repos and takes it off the ones this tool labelled that are no longer P0."""
    from auditor.merge_poll import github_token, list_poll_targets

    tok = token if token is not None else github_token()
    if not tok:
        raise RuntimeError("pr intel needs GITHUB_TOKEN (GraphQL has no anonymous access)")
    targets = list_poll_targets(mirrors_path)
    report = build_report(targets, github_graphql(tok))
    if label if label is not None else linchpin.labels_enabled():
        report["labels"] = linchpin.apply_labels(
            report, token=tok, output_dir=output_dir, dry_run=label_dry_run,
        )
    path = report_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file per call, so overlapping refreshes never share (or steal) one.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(report, indent=2))
        os.chmod(tmp_name, 0o644)  # mkstemp makes it 0600; readers (the API) may run as another uid
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return report


def load_report(output_dir: str | Path | None = None, *, max_age_h: float | None = None) -> dict[str, Any] | None:
    """Cached report, or None when missing, unreadable or older than *max_age_h*."""
    path = report_path(output_dir)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    if max_age_h is not None:
        age_h = days_since(str(report.get("generated_at") or ""), datetime.now(timezone.utc)) * 24
        if not report.get("generated_at") or age_h > max_age_h:
            return None
    return report


def pr_intel_enabled() -> bool:
    return os.getenv("AI_AUDIT_PR_INTEL", "1").strip().lower() not in ("0", "false", "no", "off")
