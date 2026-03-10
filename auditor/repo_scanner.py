from __future__ import annotations

from pathlib import Path

from auditor.config import RepoConfig
from auditor.repo_resolver import resolve_repo_path


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


def scan_repo(repo: RepoConfig) -> list[Path]:
    root = resolve_repo_path(repo)
    if not root.exists():
        return []

    results: list[Path] = []
    for path in root.rglob("*"):
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in SOURCE_EXTENSIONS:
            results.append(path)
    return results
