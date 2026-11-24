"""Open PRs as data, and the links their descriptions name.

Shared by ``pr_intel`` (what is stuck) and ``linchpin`` (what holds up other repos), which
``pr_intel`` calls. Pure: no GitHub access, and nothing else from ``auditor`` (#154).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# Blocker kinds that wait on another PR or issue.
WAITING = {"depends_on", "stacked"}

# ── dependency parsing ─────────────────────────────────────────────────────────

_REF = (
    r"(?:https?://github\.com/(?P<u_owner>[\w.-]+)/(?P<u_repo>[\w.-]+)/(?:pull|issues)/(?P<u_n>\d+)"
    r"|(?P<o_owner>[\w.-]+)/(?P<o_repo>[\w.-]+)#(?P<o_n>\d+)"
    r"|(?P<r_repo>[A-Za-z][\w.-]*)#(?P<r_n>\d+)"
    r"|#(?P<h_n>\d+))"
)
# A bare "after" is not a dependency ("regression introduced after #120"): only "merge/land after".
_DEP_RE = re.compile(
    r"(?<![\w-])(?P<verb>stacked on(?: top of)?|depends on|blocked (?:by|on)|requires|waiting (?:for|on)"
    r"|(?:merge|land)s? after)\s+(?:PR\s+|pull request\s+|issue\s+)?[\[`(]*" + _REF,
    re.I,
)


def parse_dependencies(body: str, *, owner: str, repo: str) -> list[dict[str, Any]]:
    """Return ``[{"slug": "owner/repo", "number": N, "verb": …}]`` named as prerequisites."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for m in _DEP_RE.finditer(body or ""):
        slug, n = _ref_from_match(m.groupdict(), owner, repo)
        key = (slug.lower(), n)
        if key in seen:
            continue
        seen.add(key)
        verb = m.group("verb").lower()
        out.append({"slug": slug, "number": int(n), "verb": "stacked" if verb.startswith("stacked") else "depends"})
    return out


# The reverse direction: "Blocks argus-lens#96" means argus-lens#96 waits on this one.
_DEPENDANT_RE = re.compile(
    r"(?<![\w-])(?P<verb>blocks|blocking|unblocks|required by|needed by|prerequisite (?:of|for))\s+"
    r"(?:PR\s+|pull request\s+|issue\s+)?[\[`(]*" + _REF,
    re.I,
)


def _ref_from_match(g: dict[str, Any], owner: str, repo: str) -> tuple[str, int]:
    if g["u_n"]:
        return f"{g['u_owner']}/{g['u_repo']}", int(g["u_n"])
    if g["o_n"]:
        return f"{g['o_owner']}/{g['o_repo']}", int(g["o_n"])
    if g["r_n"]:
        return f"{owner}/{g['r_repo']}", int(g["r_n"])
    return f"{owner}/{repo}", int(g["h_n"])


def parse_dependants(body: str, *, owner: str, repo: str) -> list[dict[str, Any]]:
    """Return ``[{"slug", "number"}]`` that the body says wait on this PR/issue."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for m in _DEPENDANT_RE.finditer(body or ""):
        slug, n = _ref_from_match(m.groupdict(), owner, repo)
        if (slug.lower(), n) in seen:
            continue
        seen.add((slug.lower(), n))
        out.append({"slug": slug, "number": n})
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
    dependants: list[dict[str, Any]] = field(default_factory=list)  # body says these wait on it
    closes: list[dict[str, Any]] = field(default_factory=list)  # issues it closes on merge
    blockers: list[Blocker] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)  # refs this PR holds up (transitively)
    state: str = "ready"  # blocked | waiting | ready | draft | pending
    next_action: str = ""
    score: float = 0.0
    p0: bool = False
    linchpin: list[str] = field(default_factory=list)  # why it is P0: cross_repo, cycle
    cross_repo: bool = False  # head branch lives in a fork, so it can never be another PR's base

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"

    def age_days(self, now: datetime) -> float:
        return days_since(self.updated_at, now)

    def to_dict(self, now: datetime) -> dict[str, Any]:
        d = asdict(self)
        d["ref"] = self.ref
        d["idle_days"] = round(self.age_days(now), 1)
        d["open_days"] = round(days_since(self.created_at, now), 1)
        return d


def _as_utc(t: datetime) -> datetime:
    """Treat a naive datetime as UTC so aware and naive values can be subtracted."""
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def days_since(iso: str | None, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max((_as_utc(now) - _as_utc(t)).total_seconds() / 86400, 0.0)


def slug_ref(slug: str, number: int) -> str:
    return f"{slug.split('/', 1)[-1]}#{number}"
