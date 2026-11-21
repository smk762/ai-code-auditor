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
    import json

    mod, _ = ingest
    monkeypatch.undo()  # the real client this time, with only its HTTP post faked (nothing is sent)
    monkeypatch.delenv("AI_AUDIT_RAG_COLLECTION", raising=False)
    monkeypatch.setenv("INGEST_SHARED_SECRET", "s")
    posted: list[dict] = []

    class _Ok:
        status_code, text = 200, "{}"

    monkeypatch.setattr("requests.post", lambda url, data, headers, timeout: posted.append(json.loads(data)) or _Ok())
    assert mod._ingest_document("http://x", "s", "audit_docs", "d1", "text", {}, False) is True
    monkeypatch.setenv("AI_AUDIT_RAG_COLLECTION", "staging_docs")
    assert mod._ingest_document("http://x", "s", "staging_docs", "d2", "text", {}, False) is True
    # the client's collection, not the explicit argument, is what decides: a mismatch is refused
    monkeypatch.setenv("AI_AUDIT_RAG_COLLECTION", "other_docs")
    assert mod._ingest_document("http://x", "s", "staging_docs", "d3", "text", {}, False) is False
    assert [(b["docs"][0]["id"], b["collection"]) for b in posted] == [("d1", "audit_docs"), ("d2", "staging_docs")]


def test_endpoint_list_keeps_the_repo_document_within_its_cap(ingest) -> None:
    mod, _ = ingest
    endpoints = [f"GET /api/v1/resource_{i:04d}" for i in range(500)]
    doc = mod._with_endpoints("briefing " * 400, endpoints, max_chars=6000)
    assert len(doc) <= 6000
    assert doc.rstrip().endswith("more")
    shown = doc.count("GET /api/v1/resource_")
    assert f"… and {500 - shown} more" in doc


def test_a_short_endpoint_list_is_appended_whole(ingest) -> None:
    mod, _ = ingest
    doc = mod._with_endpoints("b", ["GET /b", "GET /a", "GET /a"])
    assert doc == "b\n\nAll discovered endpoints:\n  GET /a\n  GET /b"
    assert mod._with_endpoints("b", []) == "b"
