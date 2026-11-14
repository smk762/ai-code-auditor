"""Is this the same issue, reworded? Title matching for LLM findings within one file.

LLM findings are reworded from run to run, so an exact title match misses most repeats. Two
findings in the same file count as the same issue when their titles share enough words:
Jaccard ≥ 0.3 over significant words, or ≥ 0.2 when they are within 10 lines. Measured on
real nightly runs: this pairs ~30% of exact-title churn, and spot checks of the pairs were
all the same issue. It is tuned for *different runs*; within one run, two near-identical
titles in a file are often two distinct bugs, so callers don't pair findings of one run.

Used by the findings page (run-to-run delta) and by agent pack emission (dedupe).
"""
from __future__ import annotations

import re

REWORD_JACCARD = 0.3
REWORD_JACCARD_NEAR = 0.2
REWORD_NEAR_LINES = 10
_STOPWORDS = frozenset(
    "the and for with when that from into not are its but can any has was via all only which this "
    "than then their they use uses using without instead".split()
)


def title_tokens(title: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9_]{3,}", (title or "").lower())) - _STOPWORDS


def reworded_score(title_a: str, title_b: str, line_a: int | None = None, line_b: int | None = None) -> float:
    """Title similarity if the two titles look like one issue reworded, else 0.

    The near-lines allowance only applies when both lines are known (> 0)."""
    ta, tb = title_tokens(title_a), title_tokens(title_b)
    j = len(ta & tb) / max(1, len(ta | tb))
    if j >= REWORD_JACCARD:
        return j
    near = bool(line_a and line_b) and abs(int(line_a) - int(line_b)) <= REWORD_NEAR_LINES
    return j if near and j >= REWORD_JACCARD_NEAR else 0.0
