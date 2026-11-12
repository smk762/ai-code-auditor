"""Tests for the service topology renderer."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from auditor.contracts import ObservedCallEdge, RuleViolation
from auditor.service_topology import write_service_topology


_RULES = {
    "service_roles": [
        {"name": "kimini-api", "role": "gateway"},
        {"name": "gothmog", "role": "orchestrator"},
        {"name": "imogen", "role": "worker"},
        {"name": "somnus", "role": "ui"},
        {"name": "sauron", "role": "monitoring"},
    ],
    "call_graph_rules": {
        "allowed_calls": [
            {"from": "kimini-api", "to": "gothmog"},
            {"from": "kimini-api", "to": "imogen"},
            {"from": "sauron", "to": "*"},
        ],
    },
}


def _has_dot_binary() -> bool:
    return shutil.which("dot") is not None


_needs_dot = pytest.mark.skipif(
    not _has_dot_binary(),
    reason="graphviz `dot` binary not installed; topology renderer needs it",
)


@_needs_dot
def test_write_service_topology_produces_html_and_json(tmp_path: Path) -> None:
    observed = [
        ObservedCallEdge("kimini-api", "gothmog", allowed=True, evidence=["a.py:1 (httpx.post())"]),
        ObservedCallEdge("somnus", "imogen", allowed=False, evidence=["x.py:9 (requests.get())"]),
    ]
    violations = [
        RuleViolation(
            rule_name="ui_in_worker",
            violating_path="imogen/ui",
            entities=["imogen", "imogen/ui/page.tsx"],
            evidence="JSX in worker repo",
            severity="MEDIUM",
        ),
        RuleViolation(  # call_graph violation should NOT badge a node — it's an edge
            rule_name="call_graph_violation",
            violating_path="somnus → imogen",
            entities=["somnus", "imogen"],
            evidence="...",
        ),
    ]

    html_path = write_service_topology(
        rules=_RULES,
        observed_edges=observed,
        violations=violations,
        enabled_repos=["kimini-api", "gothmog", "imogen", "somnus"],  # sauron disabled
        output_dir=str(tmp_path),
    )

    assert html_path is not None and html_path.exists()
    html = html_path.read_text(encoding="utf-8")
    # Graphviz escapes hyphens in SVG (kimini-api → kimini&#45;api), so accept either form.
    for name in ("kimini-api", "gothmog", "imogen", "somnus", "sauron"):
        assert name in html or name.replace("-", "&#45;") in html
    assert "<svg" in html
    assert "Generated" in html

    json_path = tmp_path / "service_topology.json"
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    names = {n["name"] for n in payload["nodes"]}
    assert names == {"kimini-api", "gothmog", "imogen", "somnus", "sauron"}

    # sauron is not in enabled_repos → disabled flag set.
    sauron_node = next(n for n in payload["nodes"] if n["name"] == "sauron")
    assert sauron_node["disabled"] is True

    # ui_in_worker on imogen should produce a node badge; call_graph violation should not.
    imogen_node = next(n for n in payload["nodes"] if n["name"] == "imogen")
    assert imogen_node["violation_count"] == 1

    edge_kinds = {(e["from"], e["to"]): e["kind"] for e in payload["edges"]}
    assert edge_kinds[("kimini-api", "gothmog")] == "allowed_observed"
    assert edge_kinds[("kimini-api", "imogen")] == "allowed_policy_only"
    assert edge_kinds[("somnus", "imogen")] == "violation_observed"
    # Wildcard edge points to the phantom node.
    assert any(
        e["kind"] == "wildcard" and e["from"] == "sauron" for e in payload["edges"]
    )


def test_write_service_topology_returns_none_when_no_service_roles(tmp_path: Path) -> None:
    result = write_service_topology(
        rules={},
        observed_edges=[],
        violations=[],
        enabled_repos=[],
        output_dir=str(tmp_path),
    )
    assert result is None
    assert not (tmp_path / "service_topology.html").exists()


def test_write_service_topology_failsoft_on_render_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the SVG renderer raises, the writer logs and returns None — never propagates."""
    from auditor import service_topology as st

    def boom(*_, **__):  # noqa: ANN001
        raise RuntimeError("graphviz exploded")

    monkeypatch.setattr(st, "_render_svg", boom)

    result = st.write_service_topology(
        rules=_RULES,
        observed_edges=[],
        violations=[],
        enabled_repos=["kimini-api"],
        output_dir=str(tmp_path),
    )
    assert result is None
