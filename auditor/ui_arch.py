"""Data for the architecture & patterns page: violations, pattern proposals, extraction candidates."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

# Markdown section titles (auditor/reporter.py) → rule_name, for reports written before the
# JSON sidecar existed. graph rules share one section, so they map to the first name.
_SECTION_RULES = {
    "Call Graph Violations": "call_graph_violation",
    "Domain Boundary Violations": "domain_boundary_violation",
    "UI in Worker Violations": "ui_in_worker",
    "Competing Provider Paths": "competing_providers",
    "Graph Rule Violations": "service_boundary",
}


def _md_items(text: str) -> list[dict[str, Any]]:
    """``## Section`` / ``### Item`` / ``- Key: value`` markdown → one dict per item."""
    items: list[dict[str, Any]] = []
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith("### "):
            items.append({"_section": section, "_heading": line[4:].strip()})
        elif items and (m := re.match(r"^- ([A-Za-z ]+): (.*)$", line)):
            items[-1][m.group(1).strip().lower().replace(" ", "_")] = m.group(2).strip()
    return items


def _violations(output_dir: Path, repo_names: list[str]) -> dict[str, Any] | None:
    side = output_dir / "architecture_violations.json"
    if side.is_file():
        data = json.loads(side.read_text(encoding="utf-8"))
        rows = [{"rule": v.get("rule_name", ""), "repo": v.get("repo", ""), "path": v.get("violating_path", ""),
                 "severity": str(v.get("severity", "")).upper(), "evidence": v.get("evidence", ""),
                 "entities": v.get("entities") or []} for v in data.get("violations") or []]
        return {"source": "json", "generated_at": data.get("generated_at"), "violations": rows}
    md = output_dir / "architecture_violations.md"
    if not md.is_file():
        return None
    names = sorted(repo_names, key=len, reverse=True)
    rows = []
    for it in _md_items(md.read_text(encoding="utf-8")):
        heading = it["_heading"]
        repo = next((n for n in names if heading.startswith(n) and heading[len(n):len(n) + 1] in ("", ":", " ")), "")
        rows.append({"rule": _SECTION_RULES.get(it["_section"], it["_section"]), "repo": repo, "path": heading,
                     "severity": str(it.get("severity", "")).upper(), "evidence": it.get("evidence", ""),
                     "entities": [e.strip() for e in it.get("entities", "").split(",") if e.strip()]})
    return {"source": "markdown", "generated_at": None, "violations": rows}


def _proposals(output_dir: Path) -> list[dict[str, Any]] | None:
    md = output_dir / "pattern_proposals.md"
    if not md.is_file():
        return None
    out, cur = [], None
    for line in md.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            cur = {"name": line[3:].strip()}
            out.append(cur)
        elif cur is not None and (m := re.match(r"^- ([A-Za-z ]+): (.*)$", line)):
            cur[m.group(1).strip().lower()] = m.group(2).strip()
    return out


def _candidates(output_dir: Path, decisions_path: str | Path) -> dict[str, Any] | None:
    """Candidates with their copies and decision, plus decided ones the latest run no longer reports."""
    from auditor.agent_pack import load_pack
    from auditor.extraction import (
        load_decisions,
        pack_id,
        suggested,
        unit_key,
        units_of,
    )

    path = output_dir / "extraction_candidates.yaml"
    try:
        decisions = load_decisions(decisions_path)
        decisions_error = ""
    except (OSError, ValueError) as exc:
        decisions, decisions_error = {}, str(exc)[:300]
    data: Any = {}
    candidates_error = ""
    if path.is_file():
        try:  # a half-written report is shown as an error, not a crash that hides the decisions too
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            candidates_error = f"{path.name} unreadable: {exc}"[:300]
        if not isinstance(data, dict):
            data, candidates_error = {}, f"{path.name} is not a mapping"
    elif not decisions and not decisions_error:  # an unreadable decisions file is reported, not "no data"
        return None

    def pack_of(cid: str) -> dict[str, Any] | None:
        pk = load_pack(output_dir, pack_id(cid))
        if not pk:
            return None
        ext = (pk.get("governance") or {}).get("extraction") or {}
        return {"id": pack_id(cid), "status": pk.get("status") or "open", "left": ext.get("left"),
                "copies": ext.get("copies") or []}

    def row(cid: str, c: dict[str, Any], d: dict[str, Any] | None, reported: bool) -> dict[str, Any]:
        units = units_of(c) if c else list((d or {}).get("all_units") or [])
        chosen = {unit_key(u) for u in (d or {}).get("units") or []}
        return {
            "id": cid, "title": c.get("title") or (d or {}).get("title"), "domain": c.get("domain"),
            "priority": c.get("priority"), "score": c.get("extraction_score", (d or {}).get("score")),
            "repos": c.get("repos_involved") or (d or {}).get("repos") or [],
            "units": [{**u, "key": unit_key(u), "suggested": suggested(u), "chosen": unit_key(u) in chosen}
                      for u in units],
            "rationale": c.get("rationale"), "risks": c.get("risks"), "reported": reported,
            "copy_risk": c.get("copy_risk"),
            "decision": d, "pack": pack_of(cid) if d else None,
        }

    rows = []
    for c in data.get("candidates") or []:
        if isinstance(c, dict) and c.get("id"):
            rows.append(row(str(c["id"]), c, decisions.get(str(c["id"])), True))
    seen = {r["id"] for r in rows}
    rows += [row(cid, {}, d, False) for cid, d in decisions.items() if cid not in seen]
    return {"generated_at": data.get("generated_at"), "candidates": rows, "decisions_path": str(decisions_path),
            "decisions_error": decisions_error, "candidates_error": candidates_error}


def architecture_data(output_dir: str | Path, repo_names: list[str],
                      decisions_path: str | Path | None = None) -> dict[str, Any]:
    from auditor.structure_packs import ui_data as cycle_data

    out_dir = Path(output_dir)
    violations = _violations(out_dir, repo_names)
    proposals = _proposals(out_dir)
    return {
        "violations": violations,
        "violation_counts": {
            "by_rule": dict(Counter(v["rule"] for v in violations["violations"])) if violations else {},
            "by_repo": dict(Counter(v["repo"] for v in violations["violations"])) if violations else {},
        },
        "proposals": proposals,
        "proposal_counts": dict(Counter(p.get("status", "") for p in proposals)) if proposals else {},
        "extraction": _candidates(out_dir, decisions_path or _decisions_path()),
        "cycles": cycle_data(out_dir),
        "repos": sorted(repo_names),
    }


def _decisions_path() -> str:
    from auditor.extraction import decisions_path

    return decisions_path()
