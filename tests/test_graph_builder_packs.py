from __future__ import annotations

from auditor.contracts import CodeUnit
from ecosystem.graph_builder import GraphBuilder


def _unit(imports: list[str]) -> CodeUnit:
    return CodeUnit(id="u1", repo="r", file_path="r/m.py", symbol="f", kind="function", language="python",
                    start_line=1, end_line=2, ast_features={"imports": imports})


def test_reindex_drops_removed_imports(tmp_path) -> None:
    graph = GraphBuilder(str(tmp_path / "g.db"))
    try:
        graph.upsert_code_units([_unit(["os", "requests"])])
        graph.upsert_code_units([_unit(["os"])])
        assert graph.all_edges() == [("u1", "import::r::os", "IMPORTS")]
        modules = graph.conn.execute("SELECT id FROM nodes WHERE kind = 'MODULE'").fetchall()
        assert modules == [("import::r::os",)]
    finally:
        graph.close()


def test_node_metadata_is_json(tmp_path) -> None:
    import json

    graph = GraphBuilder(str(tmp_path / "g.db"))
    try:
        unit = _unit(["os"])
        unit.ast_features.update({"async": True, "returns": None, "args": ("a", "b")})
        graph.upsert_code_units([unit])
        (meta,) = graph.conn.execute("SELECT metadata FROM nodes WHERE id = 'u1'").fetchone()
        assert json.loads(meta) == {"imports": ["os"], "async": True, "returns": None, "args": ["a", "b"]}
    finally:
        graph.close()
