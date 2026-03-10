from __future__ import annotations

from auditor.chunker import extract_code_units
from auditor.config import RepoConfig
from auditor.contracts import CodeUnit, Finding
from auditor.llm_client import LLMClient
from auditor.repo_resolver import resolve_repo_path
from auditor.repo_scanner import scan_repo
from auditor.static_tools import run_static_checks


def run_repo_audit(repo: RepoConfig, llm: LLMClient | None = None) -> tuple[list[CodeUnit], list[Finding]]:
    llm = llm or LLMClient()
    resolved_root = resolve_repo_path(repo)
    files = scan_repo(repo)
    all_units: list[CodeUnit] = []
    findings: list[Finding] = []

    for file_path in files:
        units = extract_code_units(repo.name, file_path)
        all_units.extend(units)
        for unit in units:
            findings.extend(llm.analyze_code(unit))

    findings.extend(run_static_checks(repo.name, str(resolved_root)))
    return all_units, findings
