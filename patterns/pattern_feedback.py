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


def _rule_for_extraction(candidate: dict) -> dict:
    lib = candidate.get("proposed_library", {}).get("name", "shared-lib")
    applies = candidate.get("repos_involved", [])
    symbol = candidate.get("source_units", [{}])[0].get("symbols", ["custom_impl"])[0]
    return {
        "name": f"use_{lib.replace('-', '_')}",
        "description": f"All related logic should use {lib}.",
        "required_imports": [lib.replace("-", "_")],
        "forbidden_patterns": [f"custom_{symbol}"],
        "applies_to": applies,
    }


def _insert_rule_text(text: str, rule: dict) -> str | None:
    """Append *rule* to the top-level ``rules:`` list as text, keeping comments and layout.

    Returns None when the file has no block-style ``rules:`` list to append to.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "rules:"), None)
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln[0].isspace() and not ln.startswith(("-", "#")):
            end = i
            break
    # comments and blank lines just above the next top-level key belong to that key
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    block = yaml.safe_dump([rule], sort_keys=False, default_flow_style=False)
    block = "".join(f"  {ln}" if ln.strip() else ln for ln in block.splitlines(keepends=True))
    before = "".join(lines[:end])
    if before and not before.endswith("\n"):
        before += "\n"
    return before + "\n" + block + "".join(lines[end:])


def append_architecture_rule_for_extraction(candidate: dict, rules_path: str = "config/architecture_rules.yaml") -> Path:
    """Add the candidate's ``use_<lib>`` rule to the rules file (no-op if it's already there).

    The rule is inserted as text at the end of the ``rules:`` list so comments and layout
    survive; the result is re-parsed and must equal the old data plus the new rule.
    """
    path = Path(rules_path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    data = (yaml.safe_load(text) or {}) if text else {}
    rules = data.get("rules", [])
    generated = _rule_for_extraction(candidate)
    if any(r.get("name") == generated["name"] for r in rules):
        return path

    expected = {**data, "rules": [*rules, generated]}
    new_text = _insert_rule_text(text, generated) if text else None
    if new_text is None or yaml.safe_load(new_text) != expected:
        # No block-style rules list to extend: write the whole document (comments are lost).
        new_text = yaml.safe_dump(expected, sort_keys=False)
    path.write_text(new_text, encoding="utf-8")
    return path
