from __future__ import annotations

import json
import logging
import shutil
import subprocess

from auditor import rule_fixes
from auditor.contracts import Finding

logger = logging.getLogger(__name__)
# Upper bound per tool run, so a pathological file or huge repo can't stall the audit.
STATIC_TOOL_TIMEOUT_S = 900


def _run_tool(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=STATIC_TOOL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out after %ss; skipping its findings", cmd[0], STATIC_TOOL_TIMEOUT_S)
        return None


def run_static_checks(repo_name: str, repo_path: str) -> list[Finding]:
    from auditor import suite_rules, workaround

    findings: list[Finding] = []
    findings.extend(_run_bandit(repo_name, ["-r", repo_path]))
    findings.extend(_run_semgrep(repo_name, [repo_path]))
    findings.extend(suite_rules.scan(repo_name, repo_path))  # the argus-ci catalogue, as CI runs it (#55)
    findings.extend(workaround.scan(repo_name, repo_path))  # comments admitting a workaround (#60)
    return findings


def run_static_checks_on_files(repo_name: str, file_paths: list[str], *, root: str | None = None) -> list[Finding]:
    """Run static analysis on specific files (for diff-targeted auditing). With *root*, the suite
    rules run too (they key hits by path relative to the repo root)."""
    if not file_paths:
        return []
    findings: list[Finding] = []
    findings.extend(_run_bandit(repo_name, list(file_paths)))
    findings.extend(_run_semgrep(repo_name, list(file_paths)))
    if root:
        import os

        from auditor import suite_rules

        from auditor import workaround

        rel = [os.path.relpath(p, root) for p in file_paths]
        findings.extend(suite_rules.scan(repo_name, root, rel))
        findings.extend(workaround.scan(repo_name, root, rel))
    return findings


def _run_bandit(repo_name: str, target_args: list[str]) -> list[Finding]:
    if shutil.which("bandit") is None:
        return []
    cmd = ["bandit", *target_args, "-f", "json"]
    result = _run_tool(cmd)
    if result is None or result.returncode not in (0, 1):
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    return _unique_ids([
        Finding(
            id=f"bandit:{issue.get('test_id') or 'unknown'}:{issue.get('filename')}:{issue.get('line_number')}",
            type="security",
            severity=_bandit_to_severity(issue.get("issue_severity", "LOW")),
            repo=repo_name,
            file_path=issue.get("filename", ""),
            line=int(issue.get("line_number", 0)),
            title=issue.get("test_name", "Bandit issue"),
            description=issue.get("issue_text", ""),
            evidence=issue.get("code", ""),
            recommendation=rule_fixes.GENERIC,  # the brief shows the rule's own fix
            source="bandit",
        )
        for issue in payload.get("results", [])
    ])


def _run_semgrep(repo_name: str, target_args: list[str]) -> list[Finding]:
    if shutil.which("semgrep") is None:
        return []
    cmd = ["semgrep", "--config=auto", "--json", *target_args]
    result = _run_tool(cmd)
    if result is None:
        return []
    # 0 clean, 1 findings; anything else (registry unreachable, rate-limited, a config or parse
    # error) is a failed run, not a clean one, so it is never passed off silently as "nothing".
    if result.returncode not in (0, 1):
        logger.warning("semgrep failed (exit %d) for %s; its findings are missing from this scan: %s",
                       result.returncode, repo_name, (result.stderr or "").strip()[-500:])
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        logger.warning("semgrep output for %s is not JSON (%s); its findings are missing from this scan",
                       repo_name, exc)
        return []
    errors = payload.get("errors") or []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
        logger.warning("semgrep reported %d error(s) for %s, so its findings may be incomplete; first: %s",
                       len(errors), repo_name, str(first.get("message") or first.get("type") or first)[:300])
    return _unique_ids([
        Finding(
            id=f"semgrep:{issue.get('check_id') or 'unknown'}:{issue.get('path')}:{issue.get('start', {}).get('line')}",
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
    ])


def _unique_ids(findings: list[Finding]) -> list[Finding]:
    """Ids are ``<tool>:<rule>:<path>:<line>`` (the rule where ``finding_class.static_rule_id``
    reads it), so two rules on one line stay two findings. A repeat of the same rule on the same
    line keeps the first id, and later ones get ``:2``, ``:3``. Findings and packs match on repo,
    file, type and title, never on this id, so the scheme can't reopen or duplicate packs."""
    seen: dict[str, int] = {}
    for f in findings:
        n = seen[f.id] = seen.get(f.id, 0) + 1
        if n > 1:
            f.id = f"{f.id}:{n}"
    return findings


def _bandit_to_severity(value: str) -> str:
    mapping = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}
    return mapping.get(value.upper(), "LOW")


def _semgrep_to_severity(value: str) -> str:
    # legacy levels plus the newer CRITICAL/HIGH/MEDIUM/LOW; an unknown level is not minimised
    mapping = {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW",
               "CRITICAL": "CRITICAL", "HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}
    level = str(value or "").upper()
    if level not in mapping:
        logger.warning("unknown semgrep severity %r; recording it as MEDIUM", value)
    return mapping.get(level, "MEDIUM")
