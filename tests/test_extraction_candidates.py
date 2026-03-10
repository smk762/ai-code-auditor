from patterns.extraction_candidate_scorer import compute_extraction_score, priority_band


def test_priority_band_thresholds() -> None:
    assert priority_band(0.75) == "high"
    assert priority_band(0.60) == "medium"
    assert priority_band(0.40) == "low"
    assert priority_band(0.20) == "drop"


def test_extraction_score_formula() -> None:
    breakdown = {
        "similarity_score": 0.9,
        "spread_score": 0.8,
        "churn_score": 0.6,
        "quality_pressure_score": 0.4,
        "dependency_impact_score": 0.7,
        "extraction_risk_score": 0.2,
    }
    score = compute_extraction_score(breakdown)
    assert 0.0 <= score <= 1.0
    assert score > 0.5
