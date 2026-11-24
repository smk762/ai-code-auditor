"""Module cycles → a person's decision → one pack per approved cycle (#154).

The structure scan (``auditor.structure``) proposes; its suggested cuts are often the wrong place to
fix a cycle, so a cycle becomes a pack only when someone approves it on ``/ui/architecture`` and says
what to move (``NOTE_MIN`` characters or more). An agent can propose that note (``propose``); the decision
records whether it was approved as written or edited. Dismissing one keeps it out of the list while it doesn't grow.

Decisions live in ``rules/structure_decisions.json`` (``AI_AUDIT_STRUCTURE_DECISIONS_PATH``), outside
git: a private repo's paths are in them. An id is a hash of the repo and the sorted members. A cycle that changed is
matched by members: one that shares two or more with an approved cycle is that cycle, and one inside
a dismissed cycle stays dismissed. A dismissed cycle that gained a member is a new proposal.

``sync_packs`` (nightly, after a scan that finished) writes a missing approved pack, reopens an
``obsolete`` one whose cycle is back, closes an ``open`` one whose cycle the scan no longer finds (``obsolete``:
no longer reported, not a fix), and closes ``open`` cycle packs nobody approved. A repo the
scan couldn't read whole changes nothing. Only ``validate_patch`` resolves one (``structure.judge``).
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from auditor.contracts import utc_now_iso
from auditor.decisions import close_idle, load_decisions, locked, save_decisions
from auditor.structure import load_report
from auditor.structure_proposals import check, edges_key, proposals_path, view

logger = logging.getLogger(__name__)

KIND = "module_cycle"
NOTE_MIN = 20
DECISION_STATUSES = ("approved", "dismissed")


def decisions_path() -> str:
    return os.getenv("AI_AUDIT_STRUCTURE_DECISIONS_PATH", "").strip() or "rules/structure_decisions.json"


def cycle_id(repo: str, members: list[str]) -> str:
    return "cyc-" + hashlib.sha256("\n".join([repo, *sorted(members)]).encode()).hexdigest()[:10]


def pack_id(cid: str) -> str:
    return f"pack-{cid}"


def _same(decision: dict[str, Any], repo: str, members: list[str]) -> bool:
    if decision.get("repo") != repo:
        return False
    was, now = set(decision.get("members") or ()), set(members)
    return now <= was if decision.get("status") == "dismissed" else len(now & was) > 1


def rows(report: dict[str, Any] | None, decisions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each reported cycle and each decision, one row per decision. ``reported`` is None when the
    repo couldn't be read whole, so nothing is known about a cycle the scan doesn't list."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    repos = (report or {}).get("repos") or {}
    for repo, r in sorted(repos.items()):
        for c in r.get("cycles") or []:
            own = cycle_id(repo, c["members"])
            # its own decision first, then an approved one it is part of, then a dismissed one it is inside
            order = sorted(decisions, key=lambda k: (k != own, decisions[k].get("status") != "approved", k))
            cid = next((k for k in order if k == own or _same(decisions[k], repo, c["members"])), own)
            if cid in seen:  # an approved cycle that came apart into several is one row
                continue
            seen.add(cid)
            out.append({"id": cid, "repo": repo, "members": c["members"], "lazy_edges": c.get("lazy_edges"),
                        "edges": c.get("edges") or [], "cuts": c.get("cuts") or [], "reported": True,
                        "cuts_smallest": c.get("cuts_smallest"), "decision": decisions.get(cid)})
    for cid, d in sorted(decisions.items()):
        if cid not in seen:
            r = repos.get(str(d.get("repo"))) or {}
            known = bool(r) and not r.get("error") and bool(r.get("complete"))
            out.append({"id": cid, "repo": d.get("repo"), "members": d.get("members") or [], "lazy_edges": None,
                        "edges": [], "cuts": [], "cuts_smallest": None, "reported": False if known else None,
                        "decision": d})
    return out


def _proposals() -> dict[str, dict[str, Any]]:
    """Stored proposals; none when the file is unreadable, which must not stop a decision."""
    try:
        return load_decisions(proposals_path(), "proposals")
    except (OSError, ValueError) as exc:
        logger.warning("cycle proposals unreadable: %s", exc)
        return {}


def propose(output_dir: str | Path, cid: str, proposal: dict[str, Any],
            path: str | Path | None = None) -> dict[str, Any] | None:
    """Store an agent's proposal for a reported cycle. None when it isn't reported; ValueError when it
    is badly formed, written for other imports, or leaves a circle. It changes no decision and no pack."""
    p = Path(proposals_path())
    with locked(p):
        decisions = load_decisions(path or decisions_path())
        row = next((r for r in rows(load_report(output_dir), decisions) if r["id"] == cid and r["reported"]), None)
        if row is None:
            return None
        entry = {**check(proposal, row["edges"], NOTE_MIN), "repo": row["repo"], "at": utc_now_iso()}
        stored = load_decisions(p, "proposals")
        stored[cid] = entry
        save_decisions(p, stored, "proposals")
    return {"id": cid, **(view(entry, row["edges"]) or {})}


def build_pack(cid: str, d: dict[str, Any], *, public_base_url: str = "", run_id: str = ""):
    from auditor.agent_pack import AgentCodingPack

    members, cuts, pid = list(d["members"]), d.get("cuts") or [], pack_id(cid)
    first = cuts[0]["from"] if cuts else members[0]
    edges = [f"- {e['from']} → {e['to']} ({', '.join(e['names'][:6])}){' lazy' if e['lazy'] else ''}: "
             f"{', '.join(e['sites'][:2])}" for e in d.get("edges") or []]
    base = public_base_url.rstrip("/")
    return AgentCodingPack(
        work_item_id=pid, kind=KIND, priority={"severity": "MEDIUM", "score": 0.5},
        repo={"name": d["repo"], "path": "", "base_branch": "dev"},
        location={"file_path": first, "line": 0, "symbol": "", "code_snippet": "", "evidence": "\n".join(edges)[:4000]},
        narrative={
            "title": f"Break the {len(members)}-module cycle: {', '.join(Path(m).stem for m in members[:4])}"
                     + (" …" if len(members) > 4 else ""),
            "description": "\n".join([
                f"Approved {str(d.get('decided_at', ''))[:10]}. What to move, from the approver: {d['note']}", "",
                "Members:", *(f"- {m}" for m in members), "", "Imports between them:", *edges, "",
                "Fewest edges that clear it (a hint: it counts edges, and the note above wins):",
                *(f"- {c['from']} → {c['to']} ({', '.join(c['names'][:6])})" for c in cuts)]),
            "recommendation": d["note"],
            "acceptance_criteria": ["None of the members is in a module cycle", "No cycle added or moved",
                                    "The project's tests pass"],
            "missing_evidence": "",
        },
        impact={"impacted_repos": [d["repo"]], "call_path": [], "directly_changed": members},
        governance={"architecture_rules_hit": [], "related_findings": [], "structure": {"members": members},
                    "cycle_decision": {"id": cid, "note": d["note"], "decided_at": d.get("decided_at", "")}},
        verification={"suggested_test_commands": ["pytest -q --tb=short"], "tool": "pytest", "max_iterations": 3,
                      "validate_endpoint": "POST /audit/validate_patch"},
        repair={"pack_url": f"{base}/audit/packs/{pid}" if base else "", "slug": f"repair/{cid}"},
        provenance={"source": "structure_decision", "run_id": run_id, "detected_at": utc_now_iso(),
                    "handoff_path": decisions_path()},
    )


def _write(output_dir: str | Path, cid: str, d: dict[str, Any], reopen: tuple[str, ...], why: str, **kw: Any) -> str:
    """Write an approved cycle's pack and reopen it from a status in *reopen*. The caller holds ``pack_lock``."""
    from auditor.agent_pack import load_pack, write_pack

    prev = load_pack(output_dir, pack_id(cid)) or {}
    pack = build_pack(cid, d, **kw)
    write_pack(pack, output_dir)  # carries the status over
    if str(prev.get("status") or "open") in reopen:
        pack.apply_status("open", why)
        write_pack(pack, output_dir, preserve_status=False)
    return pack.status or "open"


def decide(output_dir: str | Path, cid: str, status: str, *, note: str = "", public_base_url: str = "",
           path: str | Path | None = None) -> dict[str, Any] | None:
    """Approve or dismiss a cycle. None when it is neither reported nor decided; ValueError on a bad
    request; ``DecisionConflict`` when an agent holds its pack."""
    from auditor.agent_pack import load_pack, pack_lock, rebuild_index

    note = (note or "").strip()[:2000]
    if status not in DECISION_STATUSES:
        raise ValueError(f"status must be one of {DECISION_STATUSES}")
    if status == "approved" and len(note) < NOTE_MIN:
        raise ValueError(f"say what to move ({NOTE_MIN} characters or more): the suggested cuts are often the wrong place")
    p = Path(path or decisions_path())
    with locked(p), pack_lock(output_dir):
        decisions = load_decisions(p)
        row = next((r for r in rows(load_report(output_dir), decisions) if r["id"] == cid), None)
        if row is None:
            return None
        if status == "approved" and not row["reported"]:
            raise ValueError("the latest scan doesn't report this cycle, so there is nothing to approve")
        prev = decisions.get(cid) or {}
        keep = row["decision"] is not None  # a decided cycle keeps the members it was decided on
        d = {"status": status, "repo": row["repo"], "note": note, "decided_at": utc_now_iso(),
             "members": prev["members"] if keep else row["members"],
             "edges": (prev.get("edges") if keep else None) or row["edges"],
             "cuts": (prev.get("cuts") if keep else None) or row["cuts"]}
        offer = view(_proposals().get(cid), row["edges"])
        if status == "approved" and offer and offer["offered"]:
            d["proposal"] = {"used": "as_written" if note == offer["note"] else "edited",
                             **{k: offer.get(k) for k in ("author", "at", "note")}}
        if status == "approved":
            held = (load_pack(output_dir, pack_id(cid)) or {}).get("status") or "open"
            if held in ("claimed", "in_progress", "unverified"):
                close_idle(output_dir, pack_id(cid), "obsolete", "")  # raises DecisionConflict
            out = {"pack_id": pack_id(cid), "status": _write(
                output_dir, cid, d, ("obsolete", "wontfix", "resolved"), "cycle approved", public_base_url=public_base_url)}
        else:
            had = close_idle(output_dir, pack_id(cid), "wontfix", f"cycle dismissed{': ' + note if note else ''}")
            out = {"pack_id": pack_id(cid) if had else None, "status": "wontfix" if had else None}
        decisions[cid] = d
        save_decisions(p, decisions)
        rebuild_index(output_dir, public_base_url=public_base_url)
    return {"id": cid, "decision": status, **out}


def withdraw(output_dir: str | Path, cid: str, *, public_base_url: str = "",
             path: str | Path | None = None) -> dict[str, Any] | None:
    """Undo a decision: the cycle is a proposal again and its pack (if idle) is obsolete."""
    from auditor.agent_pack import pack_lock, rebuild_index

    p = Path(path or decisions_path())
    with locked(p), pack_lock(output_dir):
        decisions = load_decisions(p)
        if cid not in decisions:
            return None
        had = close_idle(output_dir, pack_id(cid), "obsolete", "cycle approval withdrawn")
        del decisions[cid]
        save_decisions(p, decisions)
        rebuild_index(output_dir, public_base_url=public_base_url)
    return {"id": cid, "decision": None, "pack_status": "obsolete" if had else None}


def sync_packs(output_dir: str | Path, *, public_base_url: str = "", run_id: str = "",
               path: str | Path | None = None) -> dict[str, Any]:
    """Bring the packs in line with the decisions and the latest scan (see the module docstring)."""
    from auditor.agent_pack import list_packs, load_pack, pack_lock, rebuild_index

    p = Path(path or decisions_path())
    out = {"written": 0, "reopened": 0, "gone": 0, "unapproved": 0, "unknown": 0}
    with locked(p), pack_lock(output_dir):
        decisions = load_decisions(p)
        by_id = {r["id"]: r for r in rows(load_report(output_dir), decisions)}
        approved = {c: d for c, d in decisions.items() if d.get("status") == "approved"}
        for cid, d in approved.items():
            reported = by_id[cid]["reported"]
            status = (load_pack(output_dir, pack_id(cid)) or {}).get("status")
            if reported is None:
                out["unknown"] += 1
            elif reported and status in (None, "obsolete"):
                _write(output_dir, cid, d, ("obsolete",), "the cycle is on dev again",
                       public_base_url=public_base_url, run_id=run_id)
                out["written" if status is None else "reopened"] += 1
            elif not reported and status == "open":
                close_idle(output_dir, pack_id(cid), "obsolete", "the structure scan no longer finds this cycle on dev")
                out["gone"] += 1
        wanted = {pack_id(c) for c in approved}
        for entry in list_packs(output_dir, kind=KIND, status="open", limit=1_000_000):
            pid = str(entry.get("id") or "")
            if pid not in wanted and (load_pack(output_dir, pid) or {}).get("status", "open") == "open":
                close_idle(output_dir, pid, "obsolete", "not approved: cycle packs exist only for cycles approved "
                                                        "on /ui/architecture")
                out["unapproved"] += 1
        rebuild_index(output_dir, public_base_url=public_base_url)
    return out


def ui_data(output_dir: str | Path, path: str | Path | None = None) -> dict[str, Any] | None:
    from auditor.agent_pack import load_pack

    p = Path(path or decisions_path())
    try:
        decisions, error = load_decisions(p), ""
    except (OSError, ValueError) as exc:
        decisions, error = {}, str(exc)[:300]
    report = load_report(output_dir)
    if report is None and not decisions:
        return None
    out = rows(report, decisions)
    proposals = _proposals()
    for r in out:
        r["edges_key"] = edges_key(r["edges"])
        r["proposal"] = view(proposals.get(r["id"]), r["edges"]) if r["reported"] else None
        pk = load_pack(output_dir, pack_id(r["id"])) if r["decision"] else None
        r["pack"] = {"id": pack_id(r["id"]), "status": pk.get("status") or "open"} if pk else None
    unread = sorted(n for n, r in ((report or {}).get("repos") or {}).items() if r.get("error") or not r.get("complete"))
    return {"at": (report or {}).get("at"), "cycles": out, "unread": unread, "note_min": NOTE_MIN,
            "decisions_error": error}
