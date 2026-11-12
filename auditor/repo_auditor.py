from __future__ import annotations

from pathlib import Path

from auditor.chunker import extract_code_units
from auditor.config import RepoConfig
from auditor.contracts import CodeUnit, Finding
from auditor.ecosystem_briefing import build_repo_briefing
from auditor.llm_client import LLMClient
from auditor.repo_resolver import resolve_scan_root
from auditor.repo_scanner import scan_repo
from auditor.static_tools import run_static_checks


def run_repo_audit(
    repo: RepoConfig,
    llm: LLMClient | None = None,
    graph=None,
) -> tuple[list[CodeUnit], list[Finding]]:
    resolved_root = resolve_scan_root(repo)

    # Build a repo-scoped briefing and give the client a context-aware copy.
    # Each repo gets its own LLMClient so the briefing doesn't bleed across repos.
    briefing = build_repo_briefing(repo.name, resolved_root, graph=graph)
    if llm is None:
        llm = LLMClient(repo_briefing=briefing)
    else:
        llm = LLMClient(
            provider_config=llm.provider_config,
            num_gpu_layers=llm.num_gpu_layers,
            repo_briefing=briefing,
        )

    files = scan_repo(repo)
    all_units: list[CodeUnit] = []
    findings: list[Finding] = []

    for file_path in files:
        units = extract_code_units(repo.name, file_path)
        all_units.extend(units)
        for unit in units:
            findings.extend(llm.analyze_code(unit, repo_path=str(resolved_root)))

    findings.extend(run_static_checks(repo.name, str(resolved_root)))
    return all_units, findings
