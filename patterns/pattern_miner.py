from __future__ import annotations

from collections import Counter, defaultdict

from auditor.contracts import CodeUnit


def mine_patterns(units: list[CodeUnit]) -> dict[str, Counter]:
    imports_per_repo: dict[str, Counter] = defaultdict(Counter)
    for unit in units:
        for imp in unit.ast_features.get("imports", []):
            imports_per_repo[unit.repo][imp] += 1
    return imports_per_repo
