"""ArchitectureEngine: the allowed-direction check matches edge types case-insensitively, like forbidden."""
from __future__ import annotations

import pytest

from ecosystem.architecture_engine import ArchitectureEngine


class _Graph:
    def __init__(self, edges):
        self._edges = edges

    def all_edges(self):
        return self._edges


@pytest.mark.parametrize("edge_type", ["DEPENDS_ON", "depends_on", "Depends_On"])
def test_both_checks_see_a_dependency_edge_in_any_case(edge_type):
    graph = _Graph([("repo::api", "repo::db", edge_type)])
    rules = {"rules": [{"name": "dir", "allowed": ["db -> api"]},
                       {"name": "forbid", "forbidden_edges": ["api -> db:DEPENDS_ON"]}]}
    found = sorted(v.rule_name for v in ArchitectureEngine(graph, rules).evaluate())
    assert found == ["dir", "forbid"]


def test_other_edge_types_are_still_ignored_by_the_direction_check():
    graph = _Graph([("repo::api", "repo::db", "calls")])
    assert ArchitectureEngine(graph, {"rules": [{"name": "dir", "allowed": ["db -> api"]}]}).evaluate() == []
