"""scripts/ingest_ecosystem_to_rag.py must not silently send to a collection other than the one requested."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ingest(monkeypatch):
    spec = importlib.util.spec_from_file_location("ingest_ecosystem_to_rag", ROOT / "scripts" / "ingest_ecosystem_to_rag.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent: list = []
    monkeypatch.setattr("auditor.rag_client.post_documents",
                        lambda docs, **kw: sent.append(docs) or {"ingested": True, "count": len(docs)})
    monkeypatch.delenv("AI_AUDIT_RAG_COLLECTION", raising=False)
    return mod, sent


def test_a_collection_the_client_cannot_write_is_refused(ingest) -> None:
    mod, sent = ingest
    assert mod._ingest_document("http://x", "s", "staging_docs", "d1", "text", {}, False) is False
    assert sent == []


def test_the_client_collection_is_sent(ingest, monkeypatch) -> None:
    mod, sent = ingest
    assert mod._ingest_document("http://x", "s", "audit_docs", "d1", "text", {}, False) is True
    monkeypatch.setenv("AI_AUDIT_RAG_COLLECTION", "staging_docs")
    assert mod._ingest_document("http://x", "s", "staging_docs", "d2", "text", {}, False) is True
    assert [d[0]["id"] for d in sent] == ["d1", "d2"]
