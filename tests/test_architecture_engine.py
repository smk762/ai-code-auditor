from ecosystem.architecture_engine import ArchitectureEngine
from ecosystem.graph_builder import GraphBuilder


def test_architecture_engine_detects_disallowed_dependency(tmp_path) -> None:
    db_path = tmp_path / "g.db"
    graph = GraphBuilder(str(db_path))
    try:
        graph.add_dependency("service-a", "database-b")
        rules = {"rules": [{"name": "dependency_direction", "allowed": ["gateway->services"]}]}
        engine = ArchitectureEngine(graph=graph, rules=rules)
        violations = engine.evaluate()
        assert violations, "expected direction violation"
    finally:
        graph.close()


def _forbidden(tmp_path, deps: list[tuple[str, str]], patterns: list[str]) -> list[str]:
    graph = GraphBuilder(str(tmp_path / "g.db"))
    try:
        for src, tgt in deps:
            graph.add_dependency(src, tgt)
        engine = ArchitectureEngine(graph=graph, rules={"rules": [{"name": "r", "forbidden_edges": patterns}]})
        return sorted(v.violating_path for v in engine.evaluate())
    finally:
        graph.close()


def test_forbidden_edges_match_whole_names_not_substrings(tmp_path) -> None:
    deps = [("api", "db"), ("graphapi", "db"), ("api", "dbx")]
    assert _forbidden(tmp_path, deps, ["api->db:DEPENDS_ON"]) == ["repo::api -> repo::db"]
    assert _forbidden(tmp_path, deps, ["repo::api -> repo::db:DEPENDS_ON"]) == ["repo::api -> repo::db"]


def test_forbidden_edge_without_type_matches_any_type(tmp_path) -> None:
    assert _forbidden(tmp_path, [("api", "db")], ["api -> db"]) == ["repo::api -> repo::db"]
    assert _forbidden(tmp_path, [("api", "db")], ["api->db:IMPORTS"]) == []
    assert _forbidden(tmp_path, [("api", "db")], ["no arrow here"]) == []
