from __future__ import annotations

from collections import Counter
import os

from auditor.contracts import PatternProposal
from patterns.extraction_candidate_scorer import compute_extraction_score, priority_band
from patterns.extraction_migration_planner import build_public_api, phased_migration_plan, propose_library_name


def propose_patterns(scores: dict[str, int], top_n: int = 10) -> list[PatternProposal]:
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_n]
    proposals: list[PatternProposal] = []
    for pattern_name, score in ranked:
        proposals.append(
            PatternProposal(
                domain="Imports/Dependencies",
                candidates=[pattern_name],
                prevalence_by_repo={"repos_using_pattern": score},
                risk_assessment="Low risk if standardized; evaluate edge repos before enforcement.",
                recommendation=f"Standardize usage pattern: {pattern_name}",
            )
        )
    return proposals


def propose_extraction_candidates(
    duplicate_clusters: list[dict],
    graph: object,
    audit_findings: list[dict],
    git_metrics: dict,
) -> list[dict]:
    """
    Returns ranked extraction candidates with score, rationale,
    and phased migration plan.
    """
    findings_by_path = _findings_counter(audit_findings)
    ast_min = _float_env("AI_AUDIT_DUP_AST_MIN", 0.72)
    embed_min = _float_env("AI_AUDIT_DUP_EMBED_MIN", 0.78)
    sig_min = _float_env("AI_AUDIT_DUP_SIG_MIN", 0.60)
    candidates: list[dict] = []
    for idx, cluster in enumerate(duplicate_clusters, start=1):
        units = cluster.get("units", [])
        repos = cluster.get("repos", [])
        if len(repos) < 2:
            continue

        symbol = str(cluster.get("symbol", "logic"))
        domain = str(cluster.get("domain", "shared-logic"))
        package_name = propose_library_name(domain)
        metrics = git_metrics.get(cluster.get("cluster_id"), {})
        signals = cluster.get("signals", {})

        ast_similarity_score = float(signals.get("ast_similarity_score", 0.0))
        embedding_similarity_score = float(signals.get("embedding_similarity_score", 0.0))
        signature_compatibility_score = float(signals.get("signature_compatibility_score", 0.0))
        if ast_similarity_score < ast_min or embedding_similarity_score < embed_min or signature_compatibility_score < sig_min:
            continue

        similarity_score = float(signals.get("similarity_score", 0.0))
        spread_score = min(1.0, len(repos) / 6.0)
        churn_score = min(1.0, float(metrics.get("churn_90d", 0)) / 20.0)
        quality_pressure_score = min(1.0, _quality_pressure_for_units(units, findings_by_path) / 8.0)
        dependency_impact_score = min(1.0, len(repos) / 5.0)
        extraction_risk_score = _risk_score(units)

        breakdown = {
            "similarity_score": similarity_score,
            "spread_score": spread_score,
            "churn_score": churn_score,
            "quality_pressure_score": quality_pressure_score,
            "dependency_impact_score": dependency_impact_score,
            "extraction_risk_score": extraction_risk_score,
            "ast_similarity_score": ast_similarity_score,
            "embedding_similarity_score": embedding_similarity_score,
            "signature_compatibility_score": signature_compatibility_score,
        }
        score = compute_extraction_score(breakdown)
        priority = priority_band(score)
        if priority == "drop":
            continue

        symbols = sorted({u.symbol for u in units})[:6]
        source_units = [
            {"repo": u.repo, "path": u.file_path, "symbols": [u.symbol]}
            for u in units[:10]
        ]
        migration_plan = phased_migration_plan(package_name, repos)
        rationale = [
            f"Similar {symbol} logic appears across {len(repos)} repos.",
            f"Cluster churn in 90d: {metrics.get('churn_90d', 0)} changes.",
            f"Quality pressure signals across cluster files: {int(_quality_pressure_for_units(units, findings_by_path))}.",
        ]
        risks = _risk_notes(units)
        candidates.append(
            {
                "id": f"ext-{domain[:4]}-{idx:03d}",
                "title": f"Extract {symbol} into {package_name}",
                "domain": domain,
                "priority": priority,
                "extraction_score": score,
                "repos_involved": repos,
                "source_units": source_units,
                "proposed_library": {
                    "name": package_name,
                    "package_type": "python",
                    "owner_repo": repos[0],
                    "public_api": build_public_api(symbols),
                },
                "score_breakdown": breakdown,
                "rationale": rationale,
                "migration_plan": migration_plan,
                "risks": risks,
                "expected_impact": {
                    "duplicated_loc_reduction": len(units) * 25,
                    "repos_adopting": len(repos),
                    "estimated_bug_reduction": "medium" if quality_pressure_score > 0.3 else "low",
                },
            }
        )

    return sorted(candidates, key=lambda item: item["extraction_score"], reverse=True)


def _findings_counter(audit_findings: list[dict]) -> Counter:
    counter: Counter = Counter()
    for finding in audit_findings:
        path = str(finding.get("file_path", ""))
        if path:
            counter[path] += 1
    return counter


def _quality_pressure_for_units(units: list, findings_by_path: Counter) -> float:
    score = 0.0
    for unit in units:
        score += findings_by_path.get(unit.file_path, 0)
    return score


def _risk_score(units: list) -> float:
    text = " ".join((u.raw_text or "").lower()[:500] for u in units[:10])
    score = 0.15
    indicators = ["os.environ", "request", "global ", "open(", "subprocess", "socket"]
    for indicator in indicators:
        if indicator in text:
            score += 0.12
    return min(1.0, score)


def _risk_notes(units: list) -> list[str]:
    notes: list[str] = []
    text = " ".join((u.raw_text or "").lower()[:600] for u in units[:10])
    if "os.environ" in text:
        notes.append("Repo-specific configuration assumptions detected.")
    if "open(" in text or "subprocess" in text:
        notes.append("Potential IO or side-effect heavy code in candidate.")
    if "request" in text:
        notes.append("Framework/runtime coupling detected in request handling.")
    if not notes:
        notes.append("No major extraction blockers detected; validate integration tests before rollout.")
    return notes


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
