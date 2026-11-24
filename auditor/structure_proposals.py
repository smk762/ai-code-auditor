"""Agent-written fixes proposed for module cycles (#154): what to move, to be approved or edited by a person.

A proposal is text from a model, so it decides nothing: it only fills the note box on
``/ui/architecture``, and only when ``offered`` says so (a high-confidence ``fix`` for the imports as
they are now). Its claimed change must leave no circle when simulated on those imports, which trusts
the imports it says it adds; ``validate_patch`` is what proves a fix. ``edges_key`` covers who imports
which names from whom, not line numbers, so a proposal goes stale when the imports change.

Kept in ``rules/structure_proposals.json`` (``AI_AUDIT_STRUCTURE_PROPOSALS_PATH``), outside git:
it quotes private repos.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from auditor.structure import breaks_every_circle

NOTE_MAX = 2000
CONFIDENCES = ("high", "medium", "low")
VERDICTS = ("fix", "leave")


def proposals_path() -> str:
    return os.getenv("AI_AUDIT_STRUCTURE_PROPOSALS_PATH", "").strip() or "rules/structure_proposals.json"


def edges_key(edges: list[dict[str, Any]]) -> str:
    flat = sorted([e["from"], e["to"], sorted(e["names"])] for e in edges)
    return hashlib.sha256(json.dumps(flat).encode()).hexdigest()[:16]


def _pairs(value: Any, what: str) -> list[tuple[str, str]]:
    try:
        return [(str(e["from"]), str(e["to"])) for e in value or []]
    except (TypeError, KeyError) as exc:
        raise ValueError(f"{what} must be a list of {{from, to}}") from exc


def check(p: dict[str, Any], edges: list[dict[str, Any]], note_min: int) -> dict[str, Any]:
    """*p* as it will be stored; ValueError when it can't stand as a proposal for these imports."""
    if p.get("verdict") not in VERDICTS or p.get("confidence") not in CONFIDENCES:
        raise ValueError(f"verdict must be one of {VERDICTS} and confidence one of {CONFIDENCES}")
    note, why, author = (str(p.get(k) or "").strip() for k in ("note", "why", "author"))
    if not author or not note_min <= len(note) <= NOTE_MAX:
        raise ValueError(f"author is required, and a note of {note_min} to {NOTE_MAX} characters")
    key = edges_key(edges)
    if p.get("edges_key") != key:
        raise ValueError("written for other imports than the latest scan reports (edges_key): read the cycle again")
    remove, add = _pairs(p.get("remove"), "remove"), _pairs(p.get("add"), "add")
    if p["verdict"] == "fix":
        have = {(e["from"], e["to"]) for e in edges}
        if missing := set(remove) - have:
            raise ValueError(f"remove names imports the cycle doesn't have: {sorted(missing)}")
        if not breaks_every_circle((have - set(remove)) | set(add)):
            raise ValueError("the cycle is still there after removing and adding those imports")
    return {"verdict": p["verdict"], "confidence": p["confidence"], "note": note, "why": why[:NOTE_MAX],
            "author": author[:120], "edges_key": key,
            "remove": [{"from": a, "to": b} for a, b in remove], "add": [{"from": a, "to": b} for a, b in add]}


def view(p: dict[str, Any] | None, edges: list[dict[str, Any]]) -> dict[str, Any] | None:
    """*p* for the page, with ``stale`` and ``offered`` (may fill the note box)."""
    if not p:
        return None
    stale = p.get("edges_key") != edges_key(edges)
    return {**p, "stale": stale,
            "offered": not stale and p.get("verdict") == "fix" and p.get("confidence") == "high"}
