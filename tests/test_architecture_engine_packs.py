"""Regression tests for ecosystem/architecture_engine.py audit packs."""
from ecosystem.architecture_engine import ArchitectureEngine
from ecosystem.graph_builder import GraphBuilder


def _evaluate(tmp_path, deps: list[tuple[str, str]], rule: dict) -> list[tuple[str, str]]:
    graph = GraphBuilder(str(tmp_path / "g.db"))
    try:
        for src, tgt in deps:
            graph.add_dependency(src, tgt)
        engine = ArchitectureEngine(graph=graph, rules={"rules": [{"name": "r", **rule}]})
        return sorted((v.violating_path, v.evidence) for v in engine.evaluate())
    finally:
        graph.close()


def test_overlapping_forbidden_patterns_report_an_edge_once(tmp_path) -> None:
    found = _evaluate(tmp_path, [("api", "db")],
                      {"forbidden_edges": ["api->db", "api -> db:DEPENDS_ON", "repo::api->repo::db"]})
    assert [path for path, _ in found] == ["repo::api -> repo::db"]


def test_forbidden_pattern_matches_an_entity_whose_name_has_spaces(tmp_path) -> None:
    found = _evaluate(tmp_path, [("web app", "db")], {"forbidden_edges": ["web app -> db"]})
    assert [path for path, _ in found] == ["repo::web app -> repo::db"]


def test_allowed_direction_accepts_an_entity_whose_name_has_spaces(tmp_path) -> None:
    assert _evaluate(tmp_path, [("web app", "db")], {"allowed": ["web app -> db"]}) == []
    assert _evaluate(tmp_path, [("web app", "cache")], {"allowed": ["web app -> db"]}) != []
