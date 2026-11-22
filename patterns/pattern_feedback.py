from __future__ import annotations

from pathlib import Path

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
