"""Guards on request cost: what an agent or a page hits per request must not grow with the data.

Counted, not timed, so the guard is deterministic. Background: a claim once re-parsed every pack
(12 s at 4,400 packs) and the topology map ran ~8 s of regex inside the API process; both held
the GIL and stalled every other request."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import agent_pack, audit_api, ui_topology
from auditor.agent_pack import rebuild_index, write_pack
from tests.test_agent_pack import _mk_pack

HOT_AGENT_CALLS = [
    ("GET", "/audit/packs/pack-hot", None),
    ("GET", "/audit/packs/pack-hot/brief", None),
    ("GET", "/audit/packs/pack-missing", None),
    ("GET", "/audit/packs/bundle?repo=argus-cortex", None),
    ("GET", "/audit/packs?status=open", None),
    ("POST", "/audit/packs/pack-hot/status", {"status": "claimed"}),
    ("POST", "/audit/packs/pack-hot/status", {"status": "open"}),
]


def _yaml_reads(tmp_path: Path, monkeypatch, n_other: int) -> dict[str, int]:
    out = tmp_path / f"n{n_other}"
    write_pack(_mk_pack("pack-hot"), out)
    for i in range(n_other):  # growth elsewhere in the suite must not cost this repo's agents
        write_pack(_mk_pack(f"pack-other-{i}", repo="argus-lens"), out)
    rebuild_index(out)

    class _Cfg:
        output_dir = str(out)
        auth_mode = False
        auth_code_env = "AI_AUDIT_AUTH_CODE"
        repos: list = []

    monkeypatch.setattr("auditor.audit_api.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    monkeypatch.delenv("AI_AUDIT_API_TOKEN", raising=False)
    reads = {"n": 0}
    real = agent_pack._load_yaml

    def counting(text):
        reads["n"] += 1
        return real(text)

    monkeypatch.setattr(agent_pack, "_load_yaml", counting)
    client = TestClient(audit_api.app)
    costs = {}
    for method, url, body in HOT_AGENT_CALLS:
        reads["n"] = 0
        r = client.request(method, url, json=body)
        assert r.status_code in (200, 404), (url, r.text)
        costs[f"{method} {url} {body or ''}"] = reads["n"]
    return costs


def test_agent_hot_paths_cost_the_same_at_any_pack_count(tmp_path: Path, monkeypatch) -> None:
    small = _yaml_reads(tmp_path, monkeypatch, 10)
    large = _yaml_reads(tmp_path, monkeypatch, 60)
    assert small == large  # O(1) in the number of packs
    assert max(large.values()) <= 3, large


def test_topology_patterns_run_only_on_files_the_prefilter_admits(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from auditor import repo_resolver
    from wiki import sources

    seen: list[str] = []

    class Spy:
        def __init__(self, rx):
            self.rx = rx

        def search(self, text):
            seen.append(text)
            return self.rx.search(text)

    texts = {"plain.py": "x = 1\n", "net.py": "import requests\n", "llm.py": "client = Ollama()\n",
             "vec.py": "PORT = ':6333'\n"}
    for name, text in texts.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    # the real _scan, reading these files as one repo's sources
    monkeypatch.setattr(repo_resolver, "resolve_scan_root", lambda repo: tmp_path)
    monkeypatch.setattr(sources, "source_files", lambda repo: sorted(tmp_path / n for n in texts))
    monkeypatch.setattr(ui_topology, "_SERVICE_RE", {k: Spy(v) for k, v in ui_topology._SERVICE_RE.items()})
    _imports, uses, files = ui_topology._scan([SimpleNamespace(name="r")])
    assert files == {"r": 4}
    assert set(seen) == {"client = Ollama()\n", "PORT = ':6333'\n"}
    assert {sid: [e["path"] for e in ev] for sid, ev in uses["r"].items()} == {"ollama": ["llm.py"],
                                                                               "qdrant": ["vec.py"]}


@pytest.mark.parametrize("pattern", [v[3] for v in ui_topology.SERVICES.values()])
def test_topology_prefilter_admits_whatever_a_service_pattern_matches(pattern: str) -> None:
    import re

    # Each service pattern must be an alternative of the prefilter, so the prefilter never hides a match.
    assert ui_topology._scoped(pattern) in ui_topology._ANY_SERVICE_RE.pattern
    assert re.compile(ui_topology._scoped(pattern)).pattern  # compiles on its own


def test_topology_rebuilds_outside_the_api_process(monkeypatch, tmp_path: Path) -> None:
    calls = {}
    monkeypatch.setattr("auditor.ui_topology.topology_data", lambda out, **kw: calls.update(kw) or {})
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(tmp_path))
    assert TestClient(audit_api.app).get("/audit/topology/map.json").status_code == 200
    assert calls == {"isolate": True}


def test_slow_requests_are_counted_and_reported(monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_SLOW_REQUEST_MS", "0")
    monkeypatch.setattr(audit_api, "_slow_requests", {})
    client = TestClient(audit_api.app)
    client.get("/audit/health")
    body = client.get("/audit/health").json()
    assert body["slow_requests"]["GET /audit/health"]["count"] >= 1


def test_slow_by_design_routes_are_not_counted(monkeypatch) -> None:
    assert "/audit/validate_patch" in audit_api.SLOW_BY_DESIGN
    routes = {getattr(r, "path", "") for r in audit_api.app.routes}
    assert audit_api.SLOW_BY_DESIGN <= routes  # a renamed route can't silently drop its exemption


def test_validations_over_the_cap_get_429_not_a_queue(monkeypatch) -> None:
    import threading

    monkeypatch.setenv("AI_AUDIT_VALIDATE_MAX_CONCURRENT", "1")
    monkeypatch.setattr(audit_api, "_validate_slots", None)
    monkeypatch.delenv("AI_AUDIT_API_TOKEN", raising=False)
    started, release = threading.Event(), threading.Event()

    def slow(request):
        started.set()
        release.wait(10)
        return audit_api.JSONResponse({"ok": True})

    # Replace the endpoint body only; the route keeps its dependencies (auth, the slot).
    route = next(r for r in audit_api.app.routes if getattr(r, "path", "") == "/audit/validate_patch")
    monkeypatch.setattr(route.dependant, "call", slow)
    client = TestClient(audit_api.app)
    body = {"repo": "x", "patch": "p"}
    first = threading.Thread(target=lambda: client.post("/audit/validate_patch", json=body))
    first.start()
    assert started.wait(5)
    busy = client.post("/audit/validate_patch", json=body)
    assert busy.status_code == 429 and busy.headers["Retry-After"] == "30"
    release.set()
    first.join(10)
    started.clear()
    release.set()
    assert client.post("/audit/validate_patch", json=body).status_code == 200  # the slot came back


def _decision_reads(tmp_path: Path, monkeypatch, n_other: int) -> list[int]:
    """Packs parsed by each approve, dismiss and withdraw, for a cycle and an extraction candidate."""
    import yaml

    from auditor import extraction, structure, structure_packs

    root = tmp_path / f"d{n_other}"
    out = root / "reports"
    out.mkdir(parents=True)
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    monkeypatch.setenv("AI_AUDIT_STRUCTURE_DECISIONS_PATH", str(root / "s.json"))
    monkeypatch.setenv("AI_AUDIT_STRUCTURE_PROPOSALS_PATH", str(root / "p.json"))
    for i in range(n_other):
        write_pack(_mk_pack(f"pack-other-{i}"), out)
    rebuild_index(out)
    scan = structure.scan_sources({"a/__init__.py": "", "a/x.py": "from a.y import f\nL = 1\n",
                                   "a/y.py": "def f():\n    from a.x import L\n    return L\n"})
    (out / structure.REPORT_NAME).write_text(json.dumps({"repos": {"mono": scan}}))
    from tests.test_extraction import NOW, _cand

    (out / "extraction_candidates.yaml").write_text(yaml.safe_dump({"candidates": [_cand("ext-1", NOW)]}))
    reads = {"n": 0}
    real = agent_pack._load_yaml

    def counting(text):
        reads["n"] += 1
        return real(text)

    cid = structure_packs.cycle_id("mono", ["a/x.py", "a/y.py"])
    path = str(root / "e.json")
    calls = [lambda: structure_packs.decide(out, cid, "approved", note="Move L into a/limits.py, f is the real use"),
             lambda: structure_packs.decide(out, cid, "dismissed"), lambda: structure_packs.withdraw(out, cid),
             lambda: extraction.decide(out, "ext-1", "approved", path=path),
             lambda: extraction.decide(out, "ext-1", "dismissed", path=path),
             lambda: extraction.withdraw(out, "ext-1", path=path)]
    costs = []
    with monkeypatch.context() as m:
        m.setattr(agent_pack, "_load_yaml", counting)
        for call in calls:
            reads["n"] = 0
            assert call() is not None
            costs.append(reads["n"])
    by_id = {e["id"]: e["status"] for e in json.loads((out / "agent_packs" / "INDEX.json").read_text())["packs"]}
    rebuilt = {e["id"]: e["status"] for e in rebuild_index(out)["packs"]}
    assert by_id == rebuilt and by_id[structure_packs.pack_id(cid)] == "obsolete"  # the index is as a rebuild leaves it
    return costs


def test_a_decision_costs_the_same_at_any_pack_count(tmp_path: Path, monkeypatch) -> None:
    few, many = (_decision_reads(tmp_path, monkeypatch, n) for n in (2, 40))
    assert few == many
