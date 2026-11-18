from __future__ import annotations

import re

# Whole words only: a substring test sent "shutdown", "download" or "known" to
# ownership and "independent" to dependency.
_DEPENDENCY_RE = re.compile(r"\b(depend|depends|depended|depending|dependency|dependencies|dependent|dependents)\b")
_OWNERSHIP_RE = re.compile(r"\b(own|owns|owned|owner|owners|ownership)\b|where is")


def classify_query(query: str) -> str:
    q = query.lower()
    if "what breaks" in q or "impact" in q:
        return "impact"
    if _DEPENDENCY_RE.search(q):
        return "dependency"
    if _OWNERSHIP_RE.search(q):
        return "ownership"
    return "explain"
