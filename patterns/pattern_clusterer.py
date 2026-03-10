from __future__ import annotations

from collections import Counter


def cluster_patterns(mined: dict[str, Counter], min_count: int = 2) -> dict[str, list[str]]:
    clusters: dict[str, list[str]] = {}
    for repo, imports in mined.items():
        clusters[repo] = [name for name, count in imports.items() if count >= min_count]
    return clusters
