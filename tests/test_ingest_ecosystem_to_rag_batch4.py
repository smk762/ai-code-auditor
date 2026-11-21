"""scripts/ingest_ecosystem_to_rag.py: endpoint discovery and collection choice (batch-4 packs). No RAG sends."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("ingest_ecosystem_to_rag", ROOT / "scripts" / "ingest_ecosystem_to_rag.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_endpoint_discovery_skips_tests_fixtures_and_env_dirs(mod, tmp_path: Path) -> None:
    for rel in ("app/api.py", "app/test_api.py", "app/api_test.py", "conftest.py", "tests/test_x.py",
                "test/helpers.py", "app/fixtures/routes.py", "venv/lib/x.py", "node_modules/p/y.py",
                "build/lib/z.py", "dist/w.py", ".venv/v.py", "pkg/site-packages/s.py"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n")
    assert [p.relative_to(tmp_path).as_posix() for p in mod._endpoint_files(tmp_path)] == ["app/api.py"]


@pytest.mark.parametrize("env", [{"QDRANT_COLLECTION": ""}, {"QDRANT_COLLECTION": "project_docs"}, {}])
def test_the_generic_qdrant_collection_never_redirects_the_ingest(mod, monkeypatch, env) -> None:
    monkeypatch.delenv("QDRANT_COLLECTION", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = SimpleNamespace(rag_ingest_url="http://ingest:9050/ingest", rag_collection="audit_docs")
    assert mod._target(cfg) == ("http://ingest:9050/ingest", "audit_docs")


def test_an_empty_configured_collection_falls_back_to_the_default(mod, monkeypatch) -> None:
    monkeypatch.setenv("RAG_INGEST_URL", "")
    monkeypatch.setenv("MIMIRI_URL", "http://old:9050/ingest")
    url, collection = mod._target(SimpleNamespace(rag_ingest_url="http://cfg/ingest", rag_collection=" "))
    assert url == "http://old:9050/ingest" and collection == "audit_docs"
