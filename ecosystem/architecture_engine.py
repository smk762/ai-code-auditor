from __future__ import annotations

import logging
import re

from auditor.contracts import RuleViolation
from ecosystem.graph_builder import GraphBuilder

logger = logging.getLogger(__name__)

# "SRC->TGT" with an optional ":TYPE" suffix; names may themselves contain "::".
_TYPE_SUFFIX = re.compile(r"^(?P<tgt>.*[^:]):(?P<type>[A-Za-z_]+)$")


def _parse_forbidden(pattern: str) -> tuple[str, str, str | None] | None:
    """Split a forbidden pattern into (src, tgt, type); type None means any edge type."""
    text = _norm(pattern).upper()
    if "->" not in text:
        return None
    src, tgt = text.split("->", 1)
    edge_type: str | None = None
    m = _TYPE_SUFFIX.match(tgt)
    if m:
        tgt, edge_type = m.group("tgt"), m.group("type")
    if not src or not tgt:
        return None
    return src, tgt, edge_type


def _norm(text: str) -> str:
    """Patterns and entity names are compared without spaces (patterns are written "a -> b")."""
    return text.replace(" ", "")


def _names(entity: str) -> set[str]:
    """The fully-qualified and the short (``::``-stripped) name of an entity, normalised as patterns are."""
    full = _norm(entity).upper()
    return {full, full.split("::")[-1]}


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
        patterns = []
        for raw in forbidden_edges:
            parsed = _parse_forbidden(raw)
            if parsed is None:
                logger.warning("rule %r: malformed forbidden edge %r (expected SRC->TGT[:TYPE]); ignored",
                               rule_name, raw)
                continue
            patterns.append((raw, parsed))
        for src, tgt, edge_type in edges:
            src_names, tgt_names = _names(src), _names(tgt)
            for raw, parsed in patterns:
                f_src, f_tgt, f_type = parsed
                if f_type is not None and f_type != edge_type.upper():
                    continue
                if f_src in src_names and f_tgt in tgt_names:
                    violations.append(
                        RuleViolation(
                            rule_name=rule_name,
                            violating_path=f"{src} -> {tgt}",
                            entities=[src, tgt],
                            evidence=f"Matched forbidden pattern {_norm(raw).upper()}",
                        )
                    )
                    break  # one violation per edge and rule, however many patterns overlap
        return violations

    def _match_allowed_direction(
        self,
        rule_name: str,
        allowed_edges: list[str],
        edges: list[tuple[str, str, str]],
    ) -> list[RuleViolation]:
        if not allowed_edges:
            return []
        allowed = {_norm(item).lower() for item in allowed_edges}
        violations: list[RuleViolation] = []
        for src, tgt, edge_type in edges:
            if str(edge_type).upper() != "DEPENDS_ON":  # case-insensitive, as in _match_forbidden
                continue
            src_name = src.split("::")[-1].lower()
            tgt_name = tgt.split("::")[-1].lower()
            directional = f"{_norm(src_name)}->{_norm(tgt_name)}"
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
