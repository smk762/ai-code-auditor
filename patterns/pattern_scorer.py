from __future__ import annotations


def score_pattern_prevalence(clusters: dict[str, list[str]]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for repo, patterns in clusters.items():
        for pat in patterns:
            scores[pat] = scores.get(pat, 0) + 1
    return scores
