"""Analyse which files are affected by a set of changed files via import graph walking."""
from __future__ import annotations

import ast
import logging
import subprocess
from pathlib import Path

from auditor.contracts import ImpactResult

logger = logging.getLogger(__name__)

# Cache keyed by (repo_path_str, git_head_hash) → import graph
# Avoids re-walking large repos on repeated calls within the same process.
_graph_cache: dict[tuple[str, str], dict[str, set[str]]] = {}


def analyze_impact(
    repo_path: Path,
    changed_files: list[str],
    max_depth: int = 3,
) -> ImpactResult:
    """Find files that transitively import the changed files.

    Only Python files are traced.  Non-Python changed files are included in
    ``directly_changed`` but don't seed the import walk.

    Args:
        repo_path: absolute path to the repository root.
        changed_files: repo-relative paths of files in the diff.
        max_depth: maximum import hops to follow (default 3).
    """
    directly = frozenset(changed_files)
    py_changed = [f for f in changed_files if f.endswith(".py")]
    if not py_changed:
        return ImpactResult(directly_changed=directly, transitively_affected=frozenset())

    # import graph: file_rel → set of file_rel it imports from (within repo)
    import_graph = _get_import_graph(repo_path)

    # Reverse graph: file_rel → set of file_rel that import it
    reverse: dict[str, set[str]] = {}
    for src, deps in import_graph.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(src)

    # BFS outward from changed files
    affected: set[str] = set()
    frontier = set(py_changed)
    for _ in range(max_depth):
        next_frontier: set[str] = set()
        for f in frontier:
            for importer in reverse.get(f, ()):
                if importer not in affected and importer not in directly:
                    affected.add(importer)
                    next_frontier.add(importer)
        if not next_frontier:
            break
        frontier = next_frontier

    return ImpactResult(
        directly_changed=directly,
        transitively_affected=frozenset(affected),
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_import_graph(repo_path: Path) -> dict[str, set[str]]:
    git_hash = _git_head_hash(repo_path)
    cache_key = (str(repo_path), git_hash)
    if cache_key in _graph_cache:
        return _graph_cache[cache_key]
    graph = _build_import_graph(repo_path)
    if git_hash:
        _graph_cache[cache_key] = graph
    return graph


def _git_head_hash(repo_path: Path) -> str:
    if not (repo_path / ".git").exists():
        return ""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _build_import_graph(repo_path: Path) -> dict[str, set[str]]:
    """Walk all .py files and build {file_rel → set[file_rel]} import graph."""
    graph: dict[str, set[str]] = {}
    for py_file in repo_path.rglob("*.py"):
        file_rel = str(py_file.relative_to(repo_path))
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(text, filename=str(py_file))
        except (SyntaxError, OSError):
            graph[file_rel] = set()
            continue
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    resolved = _module_to_file(repo_path, alias.name)
                    if resolved:
                        imports.add(resolved)
            elif isinstance(node, ast.ImportFrom) and node.module:
                resolved = _module_to_file(repo_path, node.module)
                if resolved:
                    imports.add(resolved)
        graph[file_rel] = imports
    return graph


def _module_to_file(repo_path: Path, module_name: str) -> str | None:
    """Map a dotted module name to a repo-relative file path, or None if not found."""
    parts = module_name.split(".")
    # Try foo/bar/baz.py
    candidate = repo_path.joinpath(*parts).with_suffix(".py")
    if candidate.is_file():
        return str(candidate.relative_to(repo_path))
    # Try foo/bar/baz/__init__.py (package)
    init = repo_path.joinpath(*parts, "__init__.py")
    if init.is_file():
        return str(init.relative_to(repo_path))
    return None
