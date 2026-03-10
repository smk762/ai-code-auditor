from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import yaml

from auditor.aggregator import severity_counts
from auditor.contracts import Finding, RuleViolation


def write_nightly_repo_report(findings: list[Finding], output_dir: str = "reports") -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    counts = severity_counts(findings)
    report = out / "nightly_repo_report.md"
    lines = [
        "# Nightly Repo Report",
        "",
        "## Summary",
        f"- Total findings: {len(findings)}",
        f"- Critical: {counts['CRITICAL']}",
        f"- High: {counts['HIGH']}",
        f"- Medium: {counts['MEDIUM']}",
        f"- Low: {counts['LOW']}",
        "",
        "## Findings",
        "",
    ]
    if not findings:
        lines.append("- No findings detected.")
    for f in findings:
        lines.extend(
            [
                f"### {f.title}",
                f"- Severity: {f.severity}",
                f"- Type: {f.type}",
                f"- Repo: {f.repo}",
                f"- File: `{f.file_path}`:{f.line}",
                f"- Source: {f.source}",
                f"- Description: {f.description}",
                f"- Recommendation: {f.recommendation}",
                "",
            ]
        )
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def write_security_report(findings: list[Finding], output_dir: str = "reports") -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = out / "security_report.md"
    security = [f for f in findings if f.type == "security"]
    lines = ["# Security Report", "", f"- Security findings: {len(security)}", ""]
    for f in security:
        lines.append(f"- [{f.severity}] {f.repo} `{f.file_path}`:{f.line} - {f.title}")
    if not security:
        lines.append("- No security findings detected.")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def write_architecture_violations(violations: list[RuleViolation], output_dir: str = "reports") -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = out / "architecture_violations.md"
    lines = ["# Architecture Violations", "", f"- Violations: {len(violations)}", ""]
    for v in violations:
        lines.extend(
            [
                f"## {v.rule_name}",
                f"- Path: {v.violating_path}",
                f"- Entities: {', '.join(v.entities)}",
                f"- Evidence: {v.evidence}",
                "",
            ]
        )
    if not violations:
        lines.append("- No architecture violations detected.")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def write_extraction_candidates_yaml(
    candidates: list[dict],
    output_dir: str = "reports",
    ecosystem_name: str = "ai-code-auditor",
) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "extraction_candidates.yaml"
    payload = {
        "report_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ecosystem": ecosystem_name,
        "candidates": candidates,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def write_extraction_candidates_markdown(candidates: list[dict], output_dir: str = "reports") -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "extraction_candidates.md"
    lines = ["# Extraction Candidates", ""]
    if not candidates:
        lines.append("- No extraction candidates exceeded threshold.")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    for candidate in candidates:
        lines.extend(
            [
                f"## {candidate.get('title', 'Untitled candidate')}",
                f"- Score: {candidate.get('extraction_score', 0)}",
                f"- Priority: {candidate.get('priority', 'low')}",
                f"- Repos: {', '.join(candidate.get('repos_involved', []))}",
                "- Why extract:",
            ]
        )
        for rationale in candidate.get("rationale", []):
            lines.append(f"  - {rationale}")
        lines.append("- Proposed package API:")
        for api in candidate.get("proposed_library", {}).get("public_api", []):
            lines.append(f"  - {api}")
        lines.append("- Phased migration plan:")
        for phase_key, phase in candidate.get("migration_plan", {}).items():
            lines.append(f"  - {phase_key}: {phase.get('name', '')}")
            for task in phase.get("tasks", []):
                lines.append(f"    - {task}")
        lines.append("- Risks and rollback notes:")
        for risk in candidate.get("risks", []):
            lines.append(f"  - {risk}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
