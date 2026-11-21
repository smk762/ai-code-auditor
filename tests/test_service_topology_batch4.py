"""service_topology: violations follow this run's policy, and every edge endpoint is a node."""
from __future__ import annotations

import json
from pathlib import Path

from auditor import service_topology as st
from auditor.contracts import ObservedCallEdge

RULES = {
    "service_roles": [{"name": "a", "role": "api"}, {"name": "b", "role": "worker"}, {"name": "w", "role": "api"}],
    "call_graph_rules": {"allowed_calls": [{"from": "a", "to": "b"}, {"from": "w", "to": "*"},
                                           {"from": "a", "to": "rag-chat"}]},
}


def _sidecar(tmp_path: Path, monkeypatch, observed: list[ObservedCallEdge]) -> dict:
    drawn = {}
    monkeypatch.setattr(st, "_render_svg", lambda nodes, edges: drawn.update(nodes=nodes, edges=edges) or "<svg/>")
    assert st.write_service_topology(RULES, observed, [], ["a", "b", "w"], output_dir=str(tmp_path))
    data = json.loads((tmp_path / "service_topology.json").read_text())
    assert data["nodes"] == drawn["nodes"] and data["edges"] == drawn["edges"]
    return data


def test_a_pair_the_policy_allows_is_never_also_a_violation(tmp_path, monkeypatch):
    # edge.allowed computed from another policy snapshot (stale): this run's policy wins
    observed = [ObservedCallEdge("a", "b", allowed=False, evidence=["x.py:1"]),
                ObservedCallEdge("w", "b", allowed=False, evidence=["y.py:2"]),
                ObservedCallEdge("b", "a", allowed=False, evidence=["z.py:3"])]
    edges = _sidecar(tmp_path, monkeypatch, observed)["edges"]
    kinds = {(e["from"], e["to"], e["kind"]) for e in edges}
    assert ("a", "b", "allowed_observed") in kinds
    assert [(e["from"], e["to"]) for e in edges if e["kind"] == "violation_observed"] == [("b", "a")]


def test_every_edge_endpoint_is_a_node(tmp_path, monkeypatch):
    observed = [ObservedCallEdge("b", "qdrant", allowed=False, evidence=["q.py:1"])]
    data = _sidecar(tmp_path, monkeypatch, observed)
    names = {n["name"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["from"] in names and (e["to"] in names or e["to"] == st._WILDCARD_PHANTOM_ID), e
    unlisted = {n["name"]: n for n in data["nodes"] if n.get("unlisted")}
    assert set(unlisted) == {"qdrant", "rag-chat"}  # the violation is still drawn, to a placeholder
    assert unlisted["qdrant"]["disabled"] is True
