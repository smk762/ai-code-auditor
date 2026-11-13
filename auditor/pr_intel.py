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
* **spoken**: a TTS-safe sentence or three for the morning briefing.

Nothing here comments on, pushes to or merges a PR.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
WAITING = {"depends_on", "stacked"}

_PR_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequests(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url isDraft createdAt updatedAt baseRefName headRefName body
        mergeable mergeStateStatus reviewDecision
        author { login }
        labels(first: 10) { nodes { name } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state
          contexts(first: 50) { nodes {
            __typename
            ... on CheckRun { name conclusion status detailsUrl }
            ... on StatusContext { context state targetUrl }
          } }
        } } } }
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

# ── dependency parsing ─────────────────────────────────────────────────────────

_REF = (
    r"(?:https?://github\.com/(?P<u_owner>[\w.-]+)/(?P<u_repo>[\w.-]+)/(?:pull|issues)/(?P<u_n>\d+)"
    r"|(?P<o_owner>[\w.-]+)/(?P<o_repo>[\w.-]+)#(?P<o_n>\d+)"
    r"|(?P<r_repo>[A-Za-z][\w.-]*)#(?P<r_n>\d+)"
    r"|#(?P<h_n>\d+))"
)
_DEP_RE = re.compile(
    r"(?P<verb>stacked on(?: top of)?|depends on|blocked (?:by|on)|requires|waiting (?:for|on)"
    r"|(?:merge|land)s? after|after)\s+(?:PR\s+|pull request\s+|issue\s+)?[\[`(]*" + _REF,
    re.I,
)


def parse_dependencies(body: str, *, owner: str, repo: str) -> list[dict[str, Any]]:
    """Return ``[{"slug": "owner/repo", "number": N, "verb": …}]`` named as prerequisites."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for m in _DEP_RE.finditer(body or ""):
        g = m.groupdict()
        if g["u_n"]:
            slug, n = f"{g['u_owner']}/{g['u_repo']}", g["u_n"]
        elif g["o_n"]:
            slug, n = f"{g['o_owner']}/{g['o_repo']}", g["o_n"]
        elif g["r_n"]:
            slug, n = f"{owner}/{g['r_repo']}", g["r_n"]
        else:
            slug, n = f"{owner}/{repo}", g["h_n"]
        key = (slug.lower(), int(n))
        if key in seen:
            continue
        seen.add(key)
        verb = m.group("verb").lower()
        out.append({"slug": slug, "number": int(n), "verb": "stacked" if verb.startswith("stacked") else "depends"})
    return out


# ── data model ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Blocker:
    kind: str
    detail: str
    ref: str = ""  # "repo#N" of the PR/issue it waits on, when there is one
    url: str = ""


@dataclass(slots=True)
class PrIntel:
    repo: str
    slug: str
    number: int
    title: str
    url: str
    author: str
    base: str
    head: str
    default_branch: str
    draft: bool
    created_at: str
    updated_at: str
    mergeable: str
    merge_state: str
    review_decision: str
    failed_checks: list[dict[str, str]] = field(default_factory=list)
    pending_checks: int = 0
    labels: list[str] = field(default_factory=list)
    release: bool = False
    deps: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)  # refs this PR holds up (transitively)
    state: str = "ready"  # blocked | waiting | ready | draft | pending
    next_action: str = ""
    score: float = 0.0

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"

    def age_days(self, now: datetime) -> float:
        return _days_since(self.updated_at, now)

    def to_dict(self, now: datetime) -> dict[str, Any]:
        d = asdict(self)
        d["ref"] = self.ref
        d["idle_days"] = round(self.age_days(now), 1)
        d["open_days"] = round(_days_since(self.created_at, now), 1)
        return d


def _days_since(iso: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max((now - t).total_seconds() / 86400, 0.0)


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
    return bool(re.match(r"\s*(release|chore\(release\))\b", title, re.I)) or (
        base in ("main", "master") and head in ("dev", "develop")
    )


def _node_to_intel(repo: str, slug: str, default_branch: str, n: dict[str, Any]) -> PrIntel:
    failed, pending = _checks(n)
    owner, name = slug.split("/", 1)
    title = str(n.get("title") or "")
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
        deps=parse_dependencies(str(n.get("body") or ""), owner=owner, repo=name),
    )


def fetch_repo_prs(
    gql: GraphQL, repo: str, slug: str, *, retries: int = 2, retry_wait_s: float = 3.0
) -> list[PrIntel]:
    """Open PRs for one repo. GitHub computes ``mergeable`` lazily and answers UNKNOWN on
    the first ask, so re-query while any PR is still UNKNOWN."""
    owner, name = slug.split("/", 1)
    prs: list[PrIntel] = []
    for attempt in range(retries + 1):
        data = gql(_PR_QUERY, {"owner": owner, "name": name})
        repo_data = data.get("repository") or {}
        default_branch = str((repo_data.get("defaultBranchRef") or {}).get("name") or "dev")
        nodes = ((repo_data.get("pullRequests") or {}).get("nodes") or [])
        prs = [_node_to_intel(repo, slug, default_branch, n) for n in nodes]
        if attempt == retries or not any(p.mergeable == "UNKNOWN" for p in prs):
            break
        time.sleep(retry_wait_s)
    return prs


# ── classification ────────────────────────────────────────────────────────────


def _slug_ref(slug: str, number: int) -> str:
    return f"{slug.split('/', 1)[-1]}#{number}"


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
    by_head = {(p.slug.lower(), p.head): p for p in prs}

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
            ref = _slug_ref(dep["slug"], dep["number"])
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
        if not ready:  # dependency cycle: break it at the highest score
            ready = remaining[:1]
        p = ready[0]
        remaining.remove(p)
        done.add(p.ref)
        action = p.next_action
        if prereq[p.ref] and action.startswith("Wait for"):
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
    if not total:
        return ""
    blocked = int(c.get("blocked") or 0)
    waiting = int(c.get("waiting") or 0)
    ready = int(c.get("ready") or 0)
    parts = [
        f"Pull requests: {total} open, {blocked} blocked, {waiting} waiting on another, "
        f"{ready} ready to merge."
    ]
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
    errors: list[dict[str, str]] = []
    for name, slug in targets:
        try:
            prs.extend(fetch_repo_prs(gql, name, slug, retry_wait_s=retry_wait_s))
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
    state_rank = {"blocked": 0, "waiting": 1, "pending": 2, "ready": 3, "draft": 4}
    prs.sort(key=lambda p: (state_rank.get(p.state, 9), -p.score, p.ref))
    counts = {"open": len(prs)}
    for s in state_rank:
        counts[s] = sum(1 for p in prs if p.state == s)
    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "repos_checked": len(targets),
        "counts": counts,
        "stale_days": STALE_DAYS,
        "stale": sum(1 for p in prs if p.age_days(now) >= STALE_DAYS),
        "prs": [p.to_dict(now) for p in prs],
        "plan": resolution_plan(prs),
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
) -> dict[str, Any]:
    """Build the report from GitHub and write ``reports/pr_intel.json`` atomically."""
    from auditor.merge_poll import github_token, list_poll_targets

    tok = token if token is not None else github_token()
    if not tok:
        raise RuntimeError("pr intel needs GITHUB_TOKEN (GraphQL has no anonymous access)")
    report = build_report(list_poll_targets(mirrors_path), github_graphql(tok))
    path = report_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    tmp.replace(path)
    return report


def load_report(output_dir: str | Path | None = None, *, max_age_h: float | None = None) -> dict[str, Any] | None:
    """Cached report, or None when missing, unreadable or older than *max_age_h*."""
    path = report_path(output_dir)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if max_age_h is not None:
        age_h = _days_since(str(report.get("generated_at") or ""), datetime.now(timezone.utc)) * 24
        if not report.get("generated_at") or age_h > max_age_h:
            return None
    return report


def pr_intel_enabled() -> bool:
    return os.getenv("AI_AUDIT_PR_INTEL", "1").strip().lower() not in ("0", "false", "no", "off")
