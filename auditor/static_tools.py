from __future__ import annotations

import json
import shutil
import subprocess

from auditor.contracts import Finding


def run_static_checks(repo_name: str, repo_path: str) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_run_bandit(repo_name, ["-r", repo_path]))
    findings.extend(_run_semgrep(repo_name, [repo_path]))
    return findings


def run_static_checks_on_files(repo_name: str, file_paths: list[str]) -> list[Finding]:
    """Run static analysis on specific files (for diff-targeted auditing)."""
    if not file_paths:
        return []
    findings: list[Finding] = []
    findings.extend(_run_bandit(repo_name, list(file_paths)))
    findings.extend(_run_semgrep(repo_name, list(file_paths)))
    return findings


def _run_bandit(repo_name: str, target_args: list[str]) -> list[Finding]:
    if shutil.which("bandit") is None:
        return []
    cmd = ["bandit", *target_args, "-f", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    return [
        Finding(
            id=f"bandit:{issue.get('filename')}:{issue.get('line_number')}",
            type="security",
            severity=_bandit_to_severity(issue.get("issue_severity", "LOW")),
            repo=repo_name,
            file_path=issue.get("filename", ""),
            line=int(issue.get("line_number", 0)),
            title=issue.get("test_name", "Bandit issue"),
            description=issue.get("issue_text", ""),
            evidence=issue.get("code", ""),
            recommendation="Review and remediate this security finding.",
            source="bandit",
        )
        for issue in payload.get("results", [])
    ]


def _run_semgrep(repo_name: str, target_args: list[str]) -> list[Finding]:
    if shutil.which("semgrep") is None:
        return []
    cmd = ["semgrep", "--config=auto", "--json", *target_args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    return [
        Finding(
            id=f"semgrep:{issue.get('path')}:{issue.get('start', {}).get('line')}",
            type="code_quality",
            severity=_semgrep_to_severity(issue.get("extra", {}).get("severity", "WARNING")),
            repo=repo_name,
            file_path=issue.get("path", ""),
            line=int(issue.get("start", {}).get("line", 0)),
            title=issue.get("check_id", "Semgrep finding"),
            description=issue.get("extra", {}).get("message", ""),
            evidence=issue.get("extra", {}).get("lines", ""),
            recommendation="Apply the Semgrep recommendation and re-run checks.",
            source="semgrep",
        )
        for issue in payload.get("results", [])
    ]


def _bandit_to_severity(value: str) -> str:
    mapping = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}
    return mapping.get(value.upper(), "LOW")


def _semgrep_to_severity(value: str) -> str:
    mapping = {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW"}
    return mapping.get(value.upper(), "LOW")
