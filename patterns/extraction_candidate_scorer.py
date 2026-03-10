from __future__ import annotations


WEIGHTS = {
    "similarity_score": 0.30,
    "spread_score": 0.20,
    "churn_score": 0.20,
    "quality_pressure_score": 0.15,
    "dependency_impact_score": 0.10,
    "extraction_risk_score": -0.15,
}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_extraction_score(breakdown: dict[str, float]) -> float:
    total = 0.0
    for key, weight in WEIGHTS.items():
        total += weight * clamp01(float(breakdown.get(key, 0.0)))
    return round(clamp01(total), 4)


def priority_band(extraction_score: float) -> str:
    if extraction_score >= 0.70:
        return "high"
    if extraction_score >= 0.50:
        return "medium"
    if extraction_score >= 0.35:
        return "low"
    return "drop"
