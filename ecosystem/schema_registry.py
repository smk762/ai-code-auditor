from __future__ import annotations

import json
from pathlib import Path


def discover_schemas(repo_path: str) -> dict[str, dict]:
    root = Path(repo_path)
    schemas: dict[str, dict] = {}
    for path in root.rglob("*.json"):
        if "schema" not in path.name.lower():
            continue
        try:
            schemas[str(path)] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return schemas
