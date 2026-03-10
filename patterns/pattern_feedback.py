from __future__ import annotations

from pathlib import Path
import yaml

from auditor.contracts import PatternProposal


def apply_feedback(proposals: list[PatternProposal], approved: set[str]) -> list[PatternProposal]:
    for proposal in proposals:
        key = proposal.candidates[0] if proposal.candidates else proposal.domain
        proposal.status = "approved" if key in approved else "pending"
    return proposals


def write_pattern_report(proposals: list[PatternProposal], output_dir: str = "reports") -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "pattern_proposals.md"
    lines = ["# Pattern Proposals", ""]
    if not proposals:
        lines.append("- No proposals generated.")
    for proposal in proposals:
        lines.extend(
            [
                f"## {proposal.candidates[0] if proposal.candidates else proposal.domain}",
                f"- Status: {proposal.status}",
                f"- Domain: {proposal.domain}",
                f"- Recommendation: {proposal.recommendation}",
                f"- Risk: {proposal.risk_assessment}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def append_architecture_rule_for_extraction(candidate: dict, rules_path: str = "config/architecture_rules.yaml") -> Path:
    path = Path(rules_path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = data.get("rules", [])

    lib = candidate.get("proposed_library", {}).get("name", "shared-lib")
    rule_name = f"use_{lib.replace('-', '_')}"
    applies = candidate.get("repos_involved", [])
    symbol = candidate.get("source_units", [{}])[0].get("symbols", ["custom_impl"])[0]
    generated = {
        "name": rule_name,
        "description": f"All related logic should use {lib}.",
        "required_imports": [lib.replace("-", "_")],
        "forbidden_patterns": [f"custom_{symbol}"],
        "applies_to": applies,
    }
    if not any(r.get("name") == rule_name for r in rules):
        rules.append(generated)
    data["rules"] = rules
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
