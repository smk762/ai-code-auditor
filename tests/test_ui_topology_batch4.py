"""ui_topology: tolerant flow-block parsing, malformed sidecar entries, a build timeout that bounds."""
from __future__ import annotations

import json
import multiprocessing
import time
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import pytest

from auditor import ui_topology

FLOW = "  quarry ──► curator\n             │\n             ▼\n           forge\n"
EXPECTED = [("quarry", "curator"), ("curator", "forge")]


@pytest.mark.parametrize("doc", [
    f"## Ecosystem flow\n\n```text\n{FLOW}```\n",
    f"## Ecosystem flow\n\n```\n{FLOW}```\n".replace("\n", "\r\n"),
    f"## Ecosystem flow\n\nHow data moves through the suite:\n\n```\n{FLOW}```\n",
], ids=["tagged_fence", "crlf", "intro_prose"])
def test_flow_block_is_found_in_common_markdown_variants(doc: str) -> None:
    assert ui_topology.parse_ecosystem_doc(doc)[1] == EXPECTED


def test_a_fence_in_a_later_section_is_not_taken_for_the_flow() -> None:
    doc = f"## Ecosystem flow\n\nTBD\n\n## Other\n\n```\n{FLOW}```\n"
    assert ui_topology.parse_ecosystem_doc(doc)[1] == []


@pytest.mark.parametrize("topo", [
    {"nodes": ["argus-forge", 3], "edges": ["x", None]},
    {"nodes": {"argus-forge": {}}, "edges": {"a": 1}},
    {"nodes": [{"name": "argus-forge", "violation_count": "lots"}],
     "edges": [{"from": ["argus-forge"], "to": "argus-quarry", "kind": "call_graph_violation"}]},
    {"nodes": [{"name": ["argus-forge"]}], "edges": [{"from": "argus-forge", "to": {"x": 1}, "kind": "violation"}]},
], ids=["scalar_entries", "mappings", "bad_count_and_unhashable_from", "unhashable_names"])
def test_malformed_sidecar_entries_add_nothing(tmp_path: Path, topo: dict) -> None:
    side = tmp_path / "service_topology.json"
    side.write_text(json.dumps(topo))
    doc = tmp_path / "eco.md"
    doc.write_text("")
    d = ui_topology._build(tmp_path, [], side, doc)
    assert [e for e in d["edges"] if e["kind"] == "violation"] == []


def test_good_sidecar_entries_survive_next_to_bad_ones(tmp_path: Path, monkeypatch) -> None:
    from auditor.config import RepoConfig

    repos = [RepoConfig(n, str(tmp_path / n)) for n in ("argus-forge", "argus-quarry")]
    for r in repos:
        Path(r.path).mkdir()
    side = tmp_path / "service_topology.json"
    side.write_text(json.dumps({
        "nodes": ["junk", {"name": "argus-forge", "violation_count": "2"}, {"name": "argus-quarry", "violation_count": "x"}],
        "edges": ["junk", {"from": "argus-forge", "to": "argus-quarry", "kind": "call_graph_violation"}]}))
    doc = tmp_path / "eco.md"
    doc.write_text("")
    d = ui_topology._build(tmp_path, repos, side, doc)
    nodes = {n["id"]: n for n in d["nodes"]}
    assert nodes["argus-forge"]["violations"] == 2 and nodes["argus-quarry"]["violations"] == 0
    assert [(e["source"], e["target"]) for e in d["edges"] if e["kind"] == "violation"] == [("argus-forge", "argus-quarry")]


def _hang(*_args):
    time.sleep(30)


def test_build_timeout_kills_the_hung_child_instead_of_waiting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ui_topology, "_build", _hang)
    monkeypatch.setattr(ui_topology, "BUILD_TIMEOUT_S", 3)
    t0 = time.monotonic()
    with pytest.raises(FutureTimeout):
        ui_topology._build_isolated(tmp_path, [], tmp_path / "service_topology.json")
    assert time.monotonic() - t0 < 15  # not the child's 30 s
    assert multiprocessing.active_children() == []  # the hung worker was terminated
