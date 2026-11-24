"""Linchpin scan: the open PRs and issues that hold up work in other repos.

Built on the PR intel graph (``auditor/pr_intel.py``), widened to open issues. An edge
``A → B`` means *A waits on B*. Edges come from:

* ``stacked``: a PR's base branch is another open PR's head ("Stacked on #N" too).
* ``body``: a description says so: "Depends on / Blocked by / Merge after X" (A waits on
  X) or "Blocks / Unblocks / Required by X" (X waits on A).
* ``native``: GitHub's issue dependencies (``blockedBy`` / ``blocking``).
* ``closes``: an open issue waits on the open PR that closes it.

A node is a **linchpin** (P0) when either:

* ``cross_repo``: nothing open holds it up, and something in *another repo* waits on it,
  directly or down a chain. Clearing it is what starts the chain moving.
* ``cycle``: it sits in a dependency cycle (A → B → … → A, often back to the repo it
  started in). A cycle never clears on its own, so every member is P0 and the report
  suggests which edge to cut.

Links in the middle of a chain are not P0: they wait on the linchpin and are listed in its
``chains``. Read-only, except :func:`apply_labels`, which is opt-in.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from auditor.pr_model import (
    WAITING,
    PrIntel,
    parse_dependants,
    parse_dependencies,
    slug_ref,
)

logger = logging.getLogger(__name__)

LEDGER_NAME = "linchpin_labels.json"
P0_SCORE_BOOST = 3.0
# Which edge to suggest cutting in a cycle: softest claim first.
EDGE_SOFTNESS = {"body": 0, "native": 1, "closes": 2, "stacked": 3}
_EDGE_WHY = {
    "body": "its description says so",
    "native": "GitHub's blocked-by link",
    "closes": "that PR closes it",
    "stacked": "its branch is stacked on it",
}


def label_name() -> str:
    return (os.getenv("AI_AUDIT_LINCHPIN_LABEL") or "P0").strip() or "P0"


def labels_enabled() -> bool:
    return os.getenv("AI_AUDIT_LINCHPIN_LABELS", "0").strip().lower() in ("1", "true", "yes", "on")


# ── issues ────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class IssueIntel:
    repo: str
    slug: str
    number: int
    title: str
    url: str
    created_at: str = ""
    updated_at: str = ""
    labels: list[str] = field(default_factory=list)
    deps: list[dict[str, Any]] = field(default_factory=list)  # body: it waits on these
    dependants: list[dict[str, Any]] = field(default_factory=list)  # body: these wait on it
    blocked_by: list[dict[str, Any]] = field(default_factory=list)  # native, with state
    blocking: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


def _native(nodes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for n in nodes or []:
        slug = str((n.get("repository") or {}).get("nameWithOwner") or "")
        if slug and n.get("number"):
            out.append({
                "slug": slug, "number": int(n["number"]), "state": str(n.get("state") or "").upper(),
                "title": str(n.get("title") or ""), "url": str(n.get("url") or ""),
            })
    return out


def issue_from_node(repo: str, slug: str, n: dict[str, Any]) -> IssueIntel:
    owner, name = slug.split("/", 1)
    body = str(n.get("body") or "")
    return IssueIntel(
        repo=repo,
        slug=slug,
        number=int(n.get("number") or 0),
        title=str(n.get("title") or ""),
        url=str(n.get("url") or ""),
        created_at=str(n.get("createdAt") or ""),
        updated_at=str(n.get("updatedAt") or ""),
        labels=[str(x.get("name")) for x in ((n.get("labels") or {}).get("nodes") or [])],
        deps=parse_dependencies(body, owner=owner, repo=name),
        dependants=parse_dependants(body, owner=owner, repo=name),
        blocked_by=_native((n.get("blockedBy") or {}).get("nodes")),
        blocking=_native((n.get("blocking") or {}).get("nodes")),
    )


# ── graph ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Node:
    ref: str
    slug: str
    number: int
    kind: str  # pr | issue
    title: str
    url: str
    labels: list[str] = field(default_factory=list)
    external: bool = False  # not in a mirrored repo: reported, never labelled
    pr: PrIntel | None = None

    @property
    def repo(self) -> str:
        return self.ref.split("#", 1)[0]


def analyse(
    prs: list[PrIntel],
    issues: list[IssueIntel],
    *,
    resolve_ref: Callable[[str, int], dict[str, Any] | None] | None = None,
    mirrored: set[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Find linchpins and cycles. Marks ``p0``/``linchpin`` on the PRs and boosts their
    score, so call it after :func:`auditor.pr_intel.classify` and before planning."""
    now = now or datetime.now(timezone.utc)
    nodes: dict[str, _Node] = {}
    for p in prs:
        nodes[p.ref] = _Node(p.ref, p.slug, p.number, "pr", p.title, p.url, list(p.labels), pr=p)
    for i in issues:
        nodes.setdefault(i.ref, _Node(i.ref, i.slug, i.number, "issue", i.title, i.url, list(i.labels)))
    if mirrored is None:
        mirrored = {n.slug.lower() for n in nodes.values()}

    def ensure(slug: str, number: int, known: dict[str, Any] | None = None) -> str | None:
        """Ref of an *open* PR/issue, adding it as an external node if needed; else None."""
        ref = slug_ref(slug, number)
        if ref in nodes:
            return ref
        info = known if known is not None else (resolve_ref(slug, number) if resolve_ref else None)
        if not info or str(info.get("state") or "").upper() != "OPEN":
            return None  # merged/closed (done) or unknown
        kind = "pr" if info.get("__typename") == "PullRequest" or "/pull/" in str(info.get("url") or "") else "issue"
        nodes[ref] = _Node(
            ref, slug, number, kind, str(info.get("title") or ""), str(info.get("url") or ""),
            external=slug.lower() not in mirrored,
        )
        return ref

    waits: dict[str, dict[str, str]] = {}

    def edge(dependant: str | None, prereq: str | None, source: str) -> None:
        if not dependant or not prereq or dependant == prereq:
            return
        cur = waits.setdefault(dependant, {}).get(prereq)
        if cur is None or EDGE_SOFTNESS[source] > EDGE_SOFTNESS[cur]:
            waits[dependant][prereq] = source  # keep the strongest evidence

    for p in prs:
        dep_by_ref = {slug_ref(d["slug"], d["number"]): d for d in p.deps}
        for b in p.blockers:
            if b.kind not in WAITING or not b.ref:
                continue
            ref = b.ref if b.ref in nodes else None
            if ref is None and b.ref in dep_by_ref:
                d = dep_by_ref[b.ref]
                ref = ensure(d["slug"], int(d["number"]))
            edge(p.ref, ref, "stacked" if b.kind == "stacked" else "body")
        for d in p.dependants:
            edge(ensure(d["slug"], int(d["number"])), p.ref, "body")
        for c in p.closes:
            iref = slug_ref(c["slug"], int(c["number"]))
            if iref in nodes and nodes[iref].kind == "issue":
                edge(iref, p.ref, "closes")
    for i in issues:
        for d in i.deps:
            edge(i.ref, ensure(d["slug"], int(d["number"])), "body")
        for d in i.dependants:
            edge(ensure(d["slug"], int(d["number"])), i.ref, "body")
        for d in i.blocked_by:
            edge(i.ref, ensure(d["slug"], d["number"], d), "native")
        for d in i.blocking:
            edge(ensure(d["slug"], d["number"], d), i.ref, "native")

    rev: dict[str, set[str]] = {}
    for a, pres in waits.items():
        for b in pres:
            rev.setdefault(b, set()).add(a)

    comps = _sccs(list(nodes), waits)
    comp_of = {r: ci for ci, members in enumerate(comps) for r in members}

    def holds(ref: str) -> list[str]:
        seen: set[str] = set()
        stack = list(rev.get(ref, ()))
        while stack:
            r = stack.pop()
            if r not in seen and r != ref:
                seen.add(r)
                stack.extend(rev.get(r, ()))
        return sorted(seen)

    cycles: list[dict[str, Any]] = []
    cycle_of: dict[str, str] = {}
    for members in comps:
        if len(members) < 2:
            continue
        cid = f"cycle-{len(cycles) + 1}"
        mset = set(members)
        inner = [
            {"from": a, "to": b, "source": s}
            for a in sorted(mset) for b, s in sorted(waits.get(a, {}).items()) if b in mset
        ]
        loop = _loop(min(members), mset, waits)
        cut = min(inner, key=lambda e: (EDGE_SOFTNESS[e["source"]], -len(holds(e["to"])), e["from"], e["to"]))
        repos = sorted({nodes[r].repo for r in members})
        cycles.append({
            "id": cid,
            "members": sorted(members),
            "repos": repos,
            "cross_repo": len(repos) > 1,
            "loop": loop,
            "edges": inner,
            "cut": {
                **cut,
                "suggestion": (
                    f"{cut['from']} waits on {cut['to']} because {_EDGE_WHY[cut['source']]}. "
                    "If that is no longer true, remove it; otherwise land them together."
                ),
            },
        })
        for r in members:
            cycle_of[r] = cid

    linchpins: list[dict[str, Any]] = []
    for ref, n in nodes.items():
        reasons: list[str] = []
        held = holds(ref)
        repos_held = sorted({nodes[h].repo for h in held} - {n.repo})
        own = comp_of[ref]
        is_root = all(comp_of[pre] == own for pre in waits.get(ref, {}))
        if ref in cycle_of:
            reasons.append("cycle")
        if is_root and repos_held:
            reasons.append("cross_repo")
        if not reasons:
            continue
        linchpins.append({
            "ref": ref,
            "slug": n.slug,
            "number": n.number,
            "kind": n.kind,
            "repo": n.repo,
            "title": n.title,
            "url": n.url,
            "external": n.external,
            "labels": n.labels,
            "reasons": reasons,
            "cycle": cycle_of.get(ref, ""),
            "holds": held,
            "repos_held": repos_held,
            "chains": _chains(ref, n.repo, nodes, rev),
            "waits_on": sorted(waits.get(ref, {})),
            "state": n.pr.state if n.pr else "open",
            "draft": bool(n.pr and n.pr.draft),
            "next_action": _next_action(n, reasons, cycle_of.get(ref, ""), cycles),
        })
        if n.pr is not None:
            n.pr.p0 = True
            n.pr.linchpin = reasons
            n.pr.score = round(n.pr.score + P0_SCORE_BOOST, 2)
    linchpins.sort(key=lambda x: ("cycle" not in x["reasons"], -len(x["repos_held"]), -len(x["holds"]), x["ref"]))

    connected = set(waits) | set(rev)
    issue_rows = [
        {
            "ref": i.ref, "repo": i.repo, "number": i.number, "title": i.title, "url": i.url,
            "labels": i.labels, "waits_on": sorted(waits.get(i.ref, {})), "holds": holds(i.ref),
            "p0": any(lp["ref"] == i.ref for lp in linchpins),
        }
        for i in issues if i.ref in connected
    ]
    edges = [
        {"from": a, "to": b, "source": s}
        for a in sorted(waits) for b, s in sorted(waits[a].items())
    ]
    return {"linchpins": linchpins, "cycles": cycles, "issues": issue_rows, "edges": edges}


def _sccs(refs: list[str], waits: dict[str, dict[str, str]]) -> list[list[str]]:
    """Tarjan, iterative (chains can be long; no recursion limit surprises)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in refs:
        if root in index:
            continue
        work = [(root, iter(sorted(waits.get(root, {}))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(waits.get(nxt, {})))))
                elif nxt in on:
                    low[v] = min(low[v], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out


def _loop(start: str, members: set[str], waits: dict[str, dict[str, str]]) -> list[str]:
    """One shortest cycle through *start*, as ``[start, …, start]``."""
    parent: dict[str, str] = {}
    q = deque([start])
    while q:
        v = q.popleft()
        for w in sorted(waits.get(v, {})):
            if w not in members:
                continue
            if w == start:
                path = [v]
                while path[-1] != start:
                    path.append(parent[path[-1]])
                return [start] + path[::-1][1:] + [start] if v != start else [start, start]
            if w not in parent:
                parent[w] = v
                q.append(w)
    return [start]


def _chains(ref: str, repo: str, nodes: dict[str, _Node], rev: dict[str, set[str]], limit: int = 3) -> list[list[str]]:
    """Shortest paths from *ref* to each dependant in another repo, longest first,
    dropping paths that are a prefix of another."""
    parent: dict[str, str] = {}
    q = deque([ref])
    seen = {ref}
    while q:
        v = q.popleft()
        for w in sorted(rev.get(v, ())):
            if w not in seen:
                seen.add(w)
                parent[w] = v
                q.append(w)
    paths = []
    for end in parent:
        if nodes[end].repo == repo:
            continue
        path = [end]
        while path[-1] != ref:
            path.append(parent[path[-1]])
        paths.append(path[::-1])
    paths.sort(key=lambda p: (-len(p), p))
    kept: list[list[str]] = []
    for p in paths:
        if not any(k[: len(p)] == p for k in kept):
            kept.append(p)
    return kept[:limit]


def _next_action(n: _Node, reasons: list[str], cycle: str, cycles: list[dict[str, Any]]) -> str:
    if "cycle" in reasons:
        c = next(c for c in cycles if c["id"] == cycle)
        return f"Break the cycle: {c['cut']['from']} waits on {c['cut']['to']}"
    if n.pr is not None:
        return n.pr.next_action
    return "Resolve it, or split out the part the other repos need"


# ── spoken ────────────────────────────────────────────────────────────────────


def _and(items: list[str]) -> str:
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def spoken_linchpins(report: dict[str, Any], *, max_items: int = 2) -> str:
    """TTS-safe: cycles first, then the cross-repo linchpins."""
    bits: list[str] = []
    for c in (report.get("cycles") or [])[:1]:
        bits.append(f"Dependency cycle: {_and(c['members'][:4])} wait on each other.")
    roots = [lp for lp in report.get("linchpins") or [] if "cycle" not in lp["reasons"]][:max_items]
    if roots:
        parts = [f"{lp['ref']} holds up {_and(lp['repos_held'][:3])}" for lp in roots]
        bits.append(f"P0 linchpin{'s' if len(roots) > 1 else ''}: " + "; ".join(parts) + ".")
    return " ".join(bits)


# ── labels (opt-in write) ─────────────────────────────────────────────────────


def _ledger_path(output_dir: str | Path | None) -> Path:
    return Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports") / LEDGER_NAME


def _rest(token: str, method: str, path: str, body: dict[str, Any] | None = None, *, timeout_s: float = 20.0) -> None:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "ai-code-auditor-linchpin",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout_s):
        pass


Rest = Callable[[str, str, dict[str, Any] | None], None]


def apply_labels(
    report: dict[str, Any],
    *,
    token: str = "",
    output_dir: str | Path | None = None,
    dry_run: bool = False,
    rest: Rest | None = None,
) -> dict[str, Any]:
    """Label linchpins in mirrored repos; unlabel ones *this tool* labelled that are no
    longer P0. A label someone put on by hand is never taken off (it is not in the ledger).

    A ledger that exists but can't be read or isn't a JSON object stops the run before any
    GitHub call, and is left as it is: treating it as empty and rewriting it would forget
    every label this tool added, so none could ever be taken off."""
    label = label_name()
    call = rest or (lambda method, path, body=None: _rest(token, method, path, body))
    path = _ledger_path(output_dir)
    out: dict[str, Any] = {"label": label, "dry_run": dry_run, "added": [], "removed": [], "errors": []}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        ledger = {}
    except (OSError, ValueError) as exc:
        ledger = exc
    if not isinstance(ledger, dict):
        why = str(ledger)[:200] if isinstance(ledger, Exception) else f"not a JSON object ({type(ledger).__name__})"
        logger.warning("linchpin label ledger %s is unreadable, labels left alone: %s", path, why)
        out["errors"].append({"ref": "*", "error": f"label ledger {path} unreadable, nothing changed: {why}"})
        return out
    old_label = ledger.get("label", label)
    applied: dict[str, str] = dict(ledger.get("applied") or {}) if old_label == label else {}
    # Renamed back to an earlier name (A -> B -> A): what this tool labelled A is ours again.
    back = (ledger.get("stale") or {}).get(label) if isinstance(ledger.get("stale"), dict) else None
    if isinstance(back, dict):
        applied = {**back, **applied}
    # Items labelled under an earlier label name: still ours to take off (#8d81471265).
    stale: dict[str, dict[str, str]] = {
        str(k): dict(v) for k, v in (ledger.get("stale") or {}).items() if isinstance(v, dict) and k != label
    }
    if old_label != label and ledger.get("applied"):
        stale.setdefault(old_label, {}).update(ledger["applied"])
    want = {
        f"{lp['slug']}#{lp['number']}": lp
        for lp in report.get("linchpins") or [] if not lp.get("external")
    }
    now = datetime.now(timezone.utc).isoformat()
    enc = urllib.parse.quote(label, safe="")
    for key, lp in want.items():
        if label in (lp.get("labels") or []) or key in applied:
            continue
        slug, n = key.rsplit("#", 1)
        try:
            if not dry_run:
                call("POST", f"/repos/{slug}/issues/{n}/labels", {"labels": [label]})
                applied[key] = now
            out["added"].append(lp["ref"])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            out["errors"].append({"ref": lp["ref"], "error": str(exc)[:200]})
    for key in sorted(set(applied) - set(want)):
        slug, n = key.rsplit("#", 1)
        try:
            if not dry_run:
                call("DELETE", f"/repos/{slug}/issues/{n}/labels/{enc}", None)
                applied.pop(key, None)
            out["removed"].append(slug_ref(slug, int(n)))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # label already gone, or the item was deleted
                applied.pop(key, None)
                out["removed"].append(slug_ref(slug, int(n)))
            else:
                out["errors"].append({"ref": slug_ref(slug, int(n)), "error": str(exc)[:200]})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            out["errors"].append({"ref": slug_ref(slug, int(n)), "error": str(exc)[:200]})
    for old, keys in sorted(stale.items()):
        old_enc = urllib.parse.quote(old, safe="")
        for key in sorted(keys):
            slug, n = key.rsplit("#", 1)
            try:
                if not dry_run:
                    call("DELETE", f"/repos/{slug}/issues/{n}/labels/{old_enc}", None)
                    keys.pop(key, None)
                out["removed"].append(slug_ref(slug, int(n)))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    keys.pop(key, None)
                    out["removed"].append(slug_ref(slug, int(n)))
                else:
                    out["errors"].append({"ref": slug_ref(slug, int(n)), "error": str(exc)[:200]})
            except (urllib.error.URLError, OSError, ValueError) as exc:
                out["errors"].append({"ref": slug_ref(slug, int(n)), "error": str(exc)[:200]})
    stale = {k: v for k, v in stale.items() if v}
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        data: dict[str, Any] = {"label": label, "applied": applied}
        if stale:
            data["stale"] = stale
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    return out
