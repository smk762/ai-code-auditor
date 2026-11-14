from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import ui_topology
from auditor.config import RepoConfig

DOC = """# Eco

| Area | Repo | Role |
|------|------|------|
| Acquisition | [argus-quarry](https://github.com/o/argus-quarry) | Input stage |
| Curation | [argus-curator](https://github.com/o/argus-curator) | Curates |
| Training bridge | [argus-forge](https://github.com/o/argus-forge) | Exports |
| Eval | [argus-proof](https://github.com/o/argus-proof) | Evaluates |
| Taxonomy / AI contract | [argus-cortex](https://github.com/o/argus-cortex) | Shared foundation |
| ComfyUI | [voluptas](https://github.com/o/voluptas) | ComfyUI node |

## Ecosystem flow

```
  quarry ──► curator ──► voluptas (ComfyUI :8188)
                 │
                 ▼
              forge ──► (train) ──► proof

  Shared AI / RAG:  argus-* ──► agent-composer (rag-chat :9150)
```
"""


def test_parse_doc_table_and_flow() -> None:
    meta, flow = ui_topology.parse_ecosystem_doc(DOC)
    assert meta["argus-forge"] == {"area": "Training bridge", "url": "https://github.com/o/argus-forge", "role": "Exports"}
    assert flow == [("quarry", "curator"), ("curator", "voluptas"), ("forge", "proof"), ("curator", "forge")]
    # "(train)" is a pass-through and the "Shared AI / RAG:" legend line is skipped


@pytest.fixture
def eco(tmp_path: Path, monkeypatch) -> Path:
    def repo(name: str, files: dict[str, str]) -> RepoConfig:
        d = tmp_path / "mirrors" / name
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        return RepoConfig(name, str(d))

    repos = [
        repo("argus-quarry", {"q.py": "import requests\n"}),
        repo("argus-curator", {"a.py": "from argus_cortex import x\nNUDENET_URL = 1\n", "b.py": "nudenet = 2\n",
                                "c.py": "COMFY = 1\n", "d.py": "comfy_url = 2\n",
                                "tests/test_x.py": "import argus_forge\nqdrant = 1\n"}),
        repo("argus-forge", {"f.py": "import argus_curator\n\nqdrant = 1\n"}),  # qdrant once: a mention, not an edge
        repo("argus-proof", {"p.py": "pass\n"}),
        repo("argus-cortex", {"c.py": "pass\n"}),
        repo("voluptas", {"v.py": "comfy = 1\n", "w.py": "comfyui = 2\n"}),  # provides ComfyUI itself
    ]

    class _Cfg:
        output_dir = str(tmp_path / "reports")

    _Cfg.repos = repos
    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    doc = tmp_path / "ecosystem.md"
    doc.write_text(DOC)
    monkeypatch.setattr(ui_topology, "ECOSYSTEM_DOC", doc)
    monkeypatch.setattr(ui_topology, "_cache", {"key": None, "data": None})
    monkeypatch.delenv("AI_AUDIT_MERGE_POLL_STATE", raising=False)
    out = tmp_path / "reports"
    out.mkdir()
    (out / "service_topology.json").write_text(json.dumps({
        "generated_at": "t",
        "nodes": [{"name": "argus-forge", "role": "worker", "violation_count": 1}],
        "edges": [{"from": "argus-forge", "to": "argus-quarry", "kind": "call_graph_violation"},
                  {"from": "*", "to": "argus-quarry", "kind": "allowed_policy_only"}]}))
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(out))
    return out


def _edges(d, kind):
    return sorted((e["source"], e["target"]) for e in d["edges"] if e["kind"] == kind)


def test_topology_edges_from_doc_code_and_policy(eco: Path) -> None:
    d = ui_topology.topology_data(eco)
    assert _edges(d, "flow") == [("argus-curator", "argus-forge"), ("argus-curator", "voluptas"),
                                 ("argus-forge", "argus-proof"), ("argus-quarry", "argus-curator")]
    # imports from tests/ are ignored
    assert _edges(d, "imports") == [("argus-curator", "argus-cortex"), ("argus-forge", "argus-curator")]
    uses = {(e["source"], e["target"]): e for e in d["edges"] if e["kind"] == "uses"}
    assert set(uses) == {("argus-curator", "svc:nudenet"), ("argus-curator", "voluptas")}  # ComfyUI → its provider
    assert uses[("argus-curator", "svc:nudenet")]["weight"] == 2
    assert uses[("argus-curator", "svc:nudenet")]["evidence"][0] == {"path": "a.py", "line": 2}
    assert [(m["source"], m["service"]) for m in d["mentions"]] == [("argus-forge", "qdrant")]
    assert _edges(d, "violation") == [("argus-forge", "argus-quarry")]  # wildcard policy edges dropped

    nodes = {n["id"]: n for n in d["nodes"]}
    assert nodes["svc:nudenet"]["side"] == "left" and "svc:comfyui" not in nodes and "svc:qdrant" not in nodes
    assert nodes["argus-quarry"]["group"] == "pipeline" and nodes["argus-cortex"]["group"] == "platform"
    assert nodes["voluptas"]["group"] == "shared" and nodes["argus-forge"]["violations"] == 1
    assert nodes["argus-curator"]["files"] == 4  # tests/ not counted
    assert d["counts"] == {"flow": 4, "imports": 2, "uses": 2, "violation": 1}


def test_topology_is_cached_until_inputs_change(eco: Path, monkeypatch) -> None:
    calls = []
    real = ui_topology._build
    monkeypatch.setattr(ui_topology, "_build", lambda *a: calls.append(1) or real(*a))
    ui_topology.topology_data(eco)
    ui_topology.topology_data(eco)
    assert len(calls) == 1
    doc = ui_topology.ECOSYSTEM_DOC
    import os
    os.utime(doc, (doc.stat().st_atime, doc.stat().st_mtime + 5))
    ui_topology.topology_data(eco)
    assert len(calls) == 2


def test_routes(client: TestClient, eco: Path) -> None:
    r = client.get("/audit/topology", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/topology"
    page = client.get("/ui/topology")
    assert page.status_code == 200 and 'aria-current="page">Topology</a>' in page.text
    assert client.get("/audit/topology/map.json").json()["counts"]["flow"] == 4
    assert client.get("/audit/topology/legacy").status_code == 404
    (eco / "service_topology.html").write_text("<html>old</html>")
    assert client.get("/audit/topology/legacy").text == "<html>old</html>"
