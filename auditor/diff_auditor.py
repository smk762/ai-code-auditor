"""Orchestrate a diff-targeted audit: parse diff → impact analysis → LLM + static checks."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from auditor.chunker import extract_code_units
from auditor.config import RepoConfig
from auditor.contracts import CodeUnit, DiffAuditResult, FileDiff, Finding, ImpactResult
from auditor.diff_parser import changed_files as diff_changed_files, hunk_new_range, parse_diff
from auditor.ecosystem_briefing import build_repo_briefing
from auditor.impact_analyzer import analyze_impact
from auditor.llm_client import LLMClient
from auditor.repo_resolver import resolve_repo_path
from auditor.static_tools import run_static_checks_on_files

logger = logging.getLogger(__name__)


def get_diff_from_branch(repo_path: Path, compare_branch: str) -> str:
    """Generate a unified diff using three-dot semantics: ``git diff <branch>...HEAD``.

    Three-dot finds the merge base between the branch and HEAD, then diffs from
    there — showing only what this branch added, ignoring unrelated upstream changes.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "diff", f"{compare_branch}...HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"git diff {compare_branch!r}...HEAD timed out after 30s "
            f"(stuck SSHFS, broken smudge filter, or hanging credential helper?)"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff {compare_branch!r}...HEAD failed: {result.stderr.strip()[:400]}"
        )
    return result.stdout


def run_diff_audit(
    repo: RepoConfig,
    diff_text: str,
    llm: LLMClient | None = None,
    graph=None,
) -> DiffAuditResult:
    """Audit only the files touched by a unified diff.

    Args:
        repo: repository config entry (provides path, name).
        diff_text: unified diff string (git diff or diff -u format).
        llm: optional shared LLMClient; a repo-scoped one is created if absent.
        graph: optional ecosystem GraphBuilder for cross-repo briefing context.
    """
    repo_path = resolve_repo_path(repo)

    # 1. Parse diff → changed files
    file_diffs = parse_diff(diff_text)
    changed = diff_changed_files(file_diffs)

    if not changed:
        return DiffAuditResult(
            repo=repo.name,
            file_diffs=[],
            impact=ImpactResult(
                directly_changed=frozenset(),
                transitively_affected=frozenset(),
            ),
            units_analyzed=[],
            findings=[],
        )

    # 2. Impact analysis — which other files import from the changed files?
    impact = analyze_impact(repo_path, changed)
    all_paths = sorted(impact.directly_changed | impact.transitively_affected)

    # 3. Extract CodeUnits from every affected file
    all_units: list[CodeUnit] = []
    for rel_path in all_paths:
        abs_path = repo_path / rel_path
        if abs_path.is_file():
            all_units.extend(extract_code_units(repo.name, abs_path))

    # 4. Filter to units whose line range overlaps with a changed hunk
    relevant_units = _filter_units_to_hunks(all_units, file_diffs)

    # 5. LLM analysis on relevant units
    briefing = build_repo_briefing(repo.name, repo_path, graph=graph)
    if llm is None:
        audit_llm = LLMClient(repo_briefing=briefing)
    else:
        audit_llm = LLMClient(
            provider_config=llm.provider_config,
            num_gpu_layers=llm.num_gpu_layers,
            repo_briefing=briefing,
        )

    findings: list[Finding] = []
    for unit in relevant_units:
        findings.extend(audit_llm.analyze_code(unit, repo_path=str(repo_path)))

    # 6. Static analysis on the directly changed files only
    changed_abs = [
        str(repo_path / p)
        for p in impact.directly_changed
        if (repo_path / p).is_file()
    ]
    findings.extend(run_static_checks_on_files(repo.name, changed_abs))

    return DiffAuditResult(
        repo=repo.name,
        file_diffs=file_diffs,
        impact=impact,
        units_analyzed=relevant_units,
        findings=findings,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _filter_units_to_hunks(
    units: list[CodeUnit],
    file_diffs: list[FileDiff],
) -> list[CodeUnit]:
    """Return units whose line range overlaps with at least one changed hunk.

    New files are fully included (every line is "changed").
    Units from transitively-affected files (not in the diff) are included wholesale
    since we want to flag any issues introduced by the change in their callers.
    """
    # Build lookup structures from the diff
    hunk_ranges: dict[str, list[tuple[int, int]]] = {}  # path → [(start, end), ...]
    new_file_paths: set[str] = set()
    diff_paths: set[str] = set()

    for fd in file_diffs:
        key = fd.new_path or fd.old_path
        if not key:
            continue
        diff_paths.add(key)
        if fd.is_new_file:
            new_file_paths.add(key)
        for h in fd.hunks:
            start, end = hunk_new_range(h)
            hunk_ranges.setdefault(key, []).append((start, end))

    result: list[CodeUnit] = []
    seen: set[str] = set()

    for unit in units:
        if unit.id in seen:
            continue
        matched_key = _match_path(unit.file_path, diff_paths)

        if matched_key is None:
            # Transitively affected file — include the whole unit
            result.append(unit)
            seen.add(unit.id)
            continue

        if matched_key in new_file_paths:
            result.append(unit)
            seen.add(unit.id)
            continue

        for h_start, h_end in hunk_ranges.get(matched_key, []):
            if _ranges_overlap(unit.start_line, unit.end_line, h_start, h_end):
                result.append(unit)
                seen.add(unit.id)
                break

    return result


def _match_path(unit_path: str, candidates: set[str]) -> str | None:
    """Match an (absolute or relative) unit path against a set of repo-relative paths."""
    for candidate in candidates:
        if unit_path == candidate or unit_path.endswith("/" + candidate):
            return candidate
    return None


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end
