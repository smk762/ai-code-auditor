"""Data for the architecture & patterns page: violations, pattern proposals, extraction candidates."""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RULES_PATH = "config/architecture_rules.yaml"


def rules_path() -> str:
    """Rules file approvals write to. In Docker, config/ is baked into the image; point this at
    the mounted checkout (e.g. /repos/ai-code-auditor/config/architecture_rules.yaml) to persist."""
    return os.getenv("AI_AUDIT_ARCH_RULES_PATH", "").strip() or DEFAULT_RULES_PATH

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


def _rule_name(candidate: dict[str, Any]) -> str:
    """The rule approval would add (mirrors patterns.pattern_feedback)."""
    lib = (candidate.get("proposed_library") or {}).get("name", "shared-lib")
    return f"use_{lib.replace('-', '_')}"


def _existing_rules(rules_path: str | Path) -> set[str]:
    path = Path(rules_path)
    if not path.is_file():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(r.get("name")) for r in data.get("rules") or [] if isinstance(r, dict)}


def _candidates(output_dir: Path, rules_path: str | Path) -> dict[str, Any] | None:
    path = output_dir / "extraction_candidates.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    have = _existing_rules(rules_path)
    rows = []
    for c in data.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        lib = c.get("proposed_library") or {}
        rows.append({
            "id": c.get("id"), "title": c.get("title"), "domain": c.get("domain"), "priority": c.get("priority"),
            "score": c.get("extraction_score"), "repos": c.get("repos_involved") or [],
            "library": lib.get("name"), "owner_repo": lib.get("owner_repo"),
            "units": len(c.get("source_units") or []), "rationale": c.get("rationale"),
            "risks": c.get("risks"), "rule_name": _rule_name(c), "approved": _rule_name(c) in have,
        })
    return {"generated_at": data.get("generated_at"), "candidates": rows}


def architecture_data(output_dir: str | Path, repo_names: list[str],
                      rules_path: str | Path = DEFAULT_RULES_PATH) -> dict[str, Any]:
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
        "extraction": _candidates(out_dir, rules_path),
        "rules_path": str(rules_path),
    }


def approve_extraction(output_dir: str | Path, candidate_id: str,
                       rules_path: str | Path = DEFAULT_RULES_PATH) -> dict[str, Any] | None:
    """Same as ``ai-audit approve-extraction``: append the candidate's rule (idempotent).

    Returns None when the candidate doesn't exist.
    """
    from patterns.pattern_feedback import append_architecture_rule_for_extraction

    path = Path(output_dir) / "extraction_candidates.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    target = next((c for c in data.get("candidates") or [] if isinstance(c, dict) and c.get("id") == candidate_id), None)
    if target is None:
        return None
    rule = _rule_name(target)
    already = rule in _existing_rules(rules_path)
    append_architecture_rule_for_extraction(target, rules_path=str(rules_path))
    return {"id": candidate_id, "rule_name": rule, "rules_path": str(rules_path), "already_present": already}
