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
    assert nodes["argus-quarry"]["group"] == "data" and nodes["argus-cortex"]["group"] == "foundation"
    assert nodes["argus-forge"]["group"] == "training" and nodes["argus-forge"]["mark"] is True
    assert nodes["voluptas"]["group"] == "external" and nodes["voluptas"]["mark"] is False
    assert nodes["argus-forge"]["violations"] == 1
    assert d["stage_colours"]["training"] == {"light": "#b33a2a", "dark": "#e57a64"}
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


# ── brand marks ──────────────────────────────────────────────────────────────

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="2" fill="#b33a2a"/></svg>'


def test_brand_stages() -> None:
    from auditor import brand

    assert brand.stage_of("argus-forge") == "training" and brand.has_mark("argus-forge")
    assert brand.stage_of("argus-cull") == "data" and not brand.has_mark("argus-cull")  # demo follows its package
    assert brand.stage_of("voluptas") is None and brand.stage_of("argus-unknown") is None


def test_mark_from_mirror_then_cache_then_github(eco: Path, monkeypatch) -> None:
    from auditor import brand

    cortex = Path(next(r.path for r in __import__("auditor.config", fromlist=["x"]).load_ecosystem_config().repos
                       if r.name == "argus-cortex"))
    calls = []

    class _Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return self.body

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        return _Resp(SVG)

    monkeypatch.setattr(brand.urllib.request, "urlopen", fake_urlopen)
    assert brand.mark_svg(eco, "forge", "dark") == SVG  # fetched from GitHub, cached
    assert calls == ["https://raw.githubusercontent.com/smk762/argus-cortex/dev/brand/packages/forge/argus-forge-dark.svg"]
    assert brand.mark_svg(eco, "forge", "dark") == SVG and len(calls) == 1  # from the cache
    local = cortex / "brand" / "packages" / "forge"
    local.mkdir(parents=True)
    (local / "argus-forge-light.svg").write_bytes(SVG.replace(b"#b33a2a", b"#000000"))
    assert b"#000000" in brand.mark_svg(eco, "forge", "light") and len(calls) == 1  # the mirror wins
    assert brand.mark_svg(eco, "cull", "light") is None  # no mark for this package
    assert brand.mark_svg(eco, "forge", "neon") is None


def test_mark_rejects_active_content(eco: Path, monkeypatch) -> None:
    from auditor import brand

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'

    monkeypatch.setattr(brand.urllib.request, "urlopen", lambda req, timeout: _Resp())
    assert brand.mark_svg(eco, "lens", "light") is None
    assert not (eco / "_brand" / "argus-lens-light.svg").exists()


def test_mark_route(client: TestClient, eco: Path, monkeypatch) -> None:
    from auditor import brand

    monkeypatch.setattr(brand, "mark_svg", lambda out, pkg, variant: SVG if pkg == "forge" else None)
    r = client.get("/audit/brand/packages/forge.svg?variant=dark")
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert client.get("/audit/brand/packages/nope.svg").status_code == 404
    assert client.get("/audit/brand/packages/forge.svg?variant=neon").status_code == 422


# ── package types and service icons ──────────────────────────────────────────

def _git_repo(path: Path, files: dict[str, str]) -> str:
    import subprocess

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin"}
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "x"], check=True, env=env)
    return str(path)


def test_facets_from_code(tmp_path: Path) -> None:
    svc = _git_repo(tmp_path / "svc", {
        "pyproject.toml": '[project]\nname = "argus-svc"\n[project.scripts]\nsvc = "svc.cli:main"\n',
        "src/svc/server.py": "import x\n\napp = FastAPI()\n",
        "web/package.json": '{"devDependencies": {"vite": "5"}}',
        "tests/test_api.py": "app = Flask(__name__)\n",  # tests don't count
    })
    facets = ui_topology._facets(svc)
    assert [f["facet"] for f in facets] == ["UI", "API", "CLI", "library"]
    assert {f["facet"]: f["evidence"] for f in facets}["API"] == "src/svc/server.py:3"
    assert ui_topology._badges(facets) == ["UI", "API"]

    infra = _git_repo(tmp_path / "infra", {"main.tf": "", ".github/workflows/plan.yml": "on: workflow_call\n"})
    assert [f["facet"] for f in ui_topology._facets(infra)] == ["infra"]  # its workflows aren't a CI product
    ci = _git_repo(tmp_path / "ci", {".github/workflows/py.yml": "on:\n  workflow_call:\n"})
    assert ui_topology._badges(ui_topology._facets(ci)) == ["CI"]
    tpl = _git_repo(tmp_path / "tpl", {"copier.yml": "x: 1\n", "pyproject.toml": '[project]\nname = "t"\n'})
    assert [f["facet"] for f in ui_topology._facets(tpl)] == ["template"]
    lib = _git_repo(tmp_path / "lib", {"pyproject.toml": '[project]\nname = "argus-lib"\n', "src/lib/x.py": "pass\n"})
    assert ui_topology._badges(ui_topology._facets(lib)) == ["library"]
    assert ui_topology._facets(str(tmp_path / "missing")) == []


def test_every_drawn_service_has_a_safe_icon() -> None:
    icons = json.loads((Path(ui_topology.__file__).parent / "static" / "service-icons.json").read_text())["icons"]
    drawn = {sid for sid, (_l, _c, _s, _rx, provider) in ui_topology.SERVICES.items() if provider is None}
    assert drawn <= set(icons)
    for body in (i["body"] for i in icons.values()):
        assert "<script" not in body.lower() and " on" not in body.lower() and body.startswith("<path")
