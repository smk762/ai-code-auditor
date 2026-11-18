from __future__ import annotations

import json
import os
from pathlib import Path

# vendored, VCS and build trees: their schema files are third-party, not the repo's own contracts
SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", "site-packages", "dist", "build", "__pycache__",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", "vendor", "vendored", "third_party",
})


def discover_schemas(repo_path: str) -> dict[str, dict]:
    root = Path(repo_path)
    schemas: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)  # never descend into them
        for name in sorted(filenames):
            if not name.endswith(".json") or "schema" not in name.lower():
                continue
            path = Path(dirpath) / name
            try:
                schemas[str(path)] = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
                continue
    return schemas
