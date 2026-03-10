from __future__ import annotations


def classify_query(query: str) -> str:
    q = query.lower()
    if "what breaks" in q or "impact" in q:
        return "impact"
    if "depend" in q:
        return "dependency"
    if "own" in q or "where is" in q:
        return "ownership"
    return "explain"
