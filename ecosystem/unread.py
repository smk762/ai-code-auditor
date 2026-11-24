"""Files the architecture detectors can't read, so their silence about a repo isn't mistaken for a fix.

``repo_dependencies``, ``analyze_call_graph`` and ``detect_competing_providers`` skip a ``.py`` file
that doesn't parse (NUL bytes, absurd nesting, or syntax newer than this interpreter's), and
``detect_ui_in_worker`` skips a file it can't open. A violation is reported per repo, not per file,
so one such file leaves the whole repo unknown: ``pack_gc`` closes no pack of it as "not reported".
"""
from __future__ import annotations

import ast
import warnings
from pathlib import Path
from typing import Any

PARSE_ERRORS = (OSError, SyntaxError, ValueError, RecursionError, MemoryError)


def unread_files(repo_files: dict[str, list[Any]], repo_roots: dict[str, str]) -> dict[str, list[str]]:
    """``{repo: [repo-relative path, …]}``, only for repos with at least one. Reads the files, writes nothing."""
    out: dict[str, list[str]] = {}
    for repo, files in repo_files.items():
        root = Path(repo_roots.get(repo) or "/").resolve()
        bad = []
        for path in map(Path, files):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                if path.suffix == ".py":
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        ast.parse(text)
            except PARSE_ERRORS:
                full = path.resolve()
                bad.append(full.relative_to(root).as_posix() if full.is_relative_to(root) else path.as_posix())
        if bad:
            out[repo] = sorted(bad)
    return out
