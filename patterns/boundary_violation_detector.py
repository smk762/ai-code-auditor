"""
Boundary violation detector.

Inspects duplicate-code clusters and flags any that place domain logic in a repo
that should not own that concern, as declared in ``config/architecture_rules.yaml``
under the ``domain_ownership`` key.

A cluster signals a boundary violation when:
  - its inferred domain matches a ``domain_ownership`` entry, AND
  - at least one repo in the cluster is listed in that entry's ``forbidden_in``

The severity is taken from the rule entry (defaults to HIGH).
"""
from __future__ import annotations

from auditor.contracts import RuleViolation


def detect_boundary_violations(
    duplicate_clusters: list[dict],
    domain_ownership: list[dict],
) -> list[RuleViolation]:
    """Return RuleViolation objects for clusters that breach domain ownership rules."""
    if not domain_ownership:
        return []

    # Index rules by domain for O(1) lookup.
    rules_by_domain: dict[str, dict] = {
        rule["domain"]: rule for rule in domain_ownership if "domain" in rule
    }

    violations: list[RuleViolation] = []
    for cluster in duplicate_clusters:
        domain = cluster.get("domain", "shared-logic")
        rule = rules_by_domain.get(domain)
        if rule is None:
            continue

        forbidden_in: set[str] = set(rule.get("forbidden_in", []))
        owner: str = rule.get("owner", "")
        severity: str = rule.get("severity", "HIGH")
        repos_in_cluster: list[str] = cluster.get("repos", [])

        offending = [r for r in repos_in_cluster if r in forbidden_in]
        if not offending:
            continue

        symbol = cluster.get("symbol", cluster.get("cluster_id", "unknown"))
        description = rule.get("description", "")
        cluster_id = cluster.get("cluster_id", "")

        for repo in offending:
            violations.append(
                RuleViolation(
                    rule_name="domain_boundary_violation",
                    violating_path=f"{repo} contains {domain!r} logic (cluster: {cluster_id})",
                    entities=[repo, owner] if owner else [repo],
                    evidence=(
                        f"Symbol '{symbol}' appears in {repo!r} but domain '{domain}' "
                        f"should be owned by '{owner}'. "
                        f"Also present in: {', '.join(r for r in repos_in_cluster if r != repo)}. "
                        f"{description}"
                    ),
                    severity=severity,
                )
            )

    return violations
