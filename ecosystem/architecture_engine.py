from __future__ import annotations

from auditor.contracts import RuleViolation
from ecosystem.graph_builder import GraphBuilder


class ArchitectureEngine:
    def __init__(self, graph: GraphBuilder, rules: dict):
        self.graph = graph
        self.rules = rules.get("rules", [])

    def evaluate(self) -> list[RuleViolation]:
        violations: list[RuleViolation] = []
        edges = self.graph.all_edges()
        for rule in self.rules:
            name = rule.get("name", "unnamed_rule")
            forbidden = rule.get("forbidden_edges", [])
            allowed = rule.get("allowed", [])
            violations.extend(self._match_forbidden(name, forbidden, edges))
            violations.extend(self._match_allowed_direction(name, allowed, edges))
        return violations

    def _match_forbidden(
        self,
        rule_name: str,
        forbidden_edges: list[str],
        edges: list[tuple[str, str, str]],
    ) -> list[RuleViolation]:
        violations: list[RuleViolation] = []
        normalized = {edge.replace(" ", "").upper() for edge in forbidden_edges}
        for src, tgt, edge_type in edges:
            candidate = f"{src}->{tgt}:{edge_type}".upper()
            for forbidden in normalized:
                if edge_type.upper() in forbidden and forbidden in candidate:
                    violations.append(
                        RuleViolation(
                            rule_name=rule_name,
                            violating_path=f"{src} -> {tgt}",
                            entities=[src, tgt],
                            evidence=f"Matched forbidden pattern {forbidden}",
                        )
                    )
        return violations

    def _match_allowed_direction(
        self,
        rule_name: str,
        allowed_edges: list[str],
        edges: list[tuple[str, str, str]],
    ) -> list[RuleViolation]:
        if not allowed_edges:
            return []
        allowed = {item.replace(" ", "").lower() for item in allowed_edges}
        violations: list[RuleViolation] = []
        for src, tgt, edge_type in edges:
            if edge_type != "DEPENDS_ON":
                continue
            src_name = src.split("::")[-1].lower()
            tgt_name = tgt.split("::")[-1].lower()
            directional = f"{src_name}->{tgt_name}"
            if directional not in allowed:
                violations.append(
                    RuleViolation(
                        rule_name=rule_name,
                        violating_path=f"{src_name} -> {tgt_name}",
                        entities=[src, tgt],
                        evidence="Dependency direction not in allowed list",
                        severity="MEDIUM",
                    )
                )
        return violations
