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
