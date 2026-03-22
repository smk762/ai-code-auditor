from __future__ import annotations

import logging
from pathlib import Path

from auditor.config import RepoConfig
from auditor.contracts import BranchDiff, ScanContext
from auditor.repo_resolver import (
    BRANCH_PREFERENCE,
    branch_exists,
    detect_checked_out_branch,
    diff_files,
    resolve_repo_path,
    resolve_scan_root,
    select_branch,
)

logger = logging.getLogger(__name__)


SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".java",
    ".rb",
    ".rs",
}

IGNORE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


def scan_repo_with_context(repo: RepoConfig) -> ScanContext:
    """Scan *repo* and build branch-diff context for early PR warnings.

    Two diff pairs are computed for local git repos:

    1. ``checked_out → dev``  — if the working tree is on a feature branch,
       surface files heading toward a PR against dev.
    2. ``dev → main/master``  — files on dev that aren't yet in production,
       useful for release-readiness checks.

    Either pair is omitted when the branches involved don't exist or are
    identical to the checked-out branch.
    """
    files = scan_repo(repo)
    repo_path = resolve_repo_path(repo)
    checked_out = detect_checked_out_branch(repo_path)
    diffs: list[BranchDiff] = []

    if not (repo_path / ".git").exists():
        return ScanContext(files=files, checked_out_branch=checked_out, diffs=diffs)

    # Resolve the canonical integration branch (dev → main → master).
    integration = select_branch(repo, repo_path)

    # 1. feature branch → dev (only when we're NOT already on the integration branch)
    if checked_out and checked_out != integration:
        changed = diff_files(repo_path, base=integration, head=checked_out)
        diffs.append(BranchDiff(base=integration, head=checked_out, changed_files=changed))
        logger.debug(
            "repo '%s': %d file(s) differ between '%s' and '%s'",
            repo.name, len(changed), integration, checked_out,
        )

    # 2. dev → main/master (only when integration branch is not already main/master)
    production = next(
        (b for b in BRANCH_PREFERENCE[1:] if branch_exists(repo_path, b)),
        None,
    )
    if production and integration != production:
        changed = diff_files(repo_path, base=production, head=integration)
        diffs.append(BranchDiff(base=production, head=integration, changed_files=changed))
        logger.debug(
            "repo '%s': %d file(s) differ between '%s' and '%s'",
            repo.name, len(changed), production, integration,
        )

    return ScanContext(files=files, checked_out_branch=checked_out, diffs=diffs)


def scan_repo(repo: RepoConfig) -> list[Path]:
    root = resolve_scan_root(repo)
    if not root.exists():
        logger.warning("repo '%s': path not found, skipping (%s)", repo.name, root)
        return []

    results: list[Path] = []
    for path in root.rglob("*"):
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in SOURCE_EXTENSIONS:
            results.append(path)
    return results
