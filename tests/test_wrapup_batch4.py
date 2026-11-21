"""conductor.wrapup: top findings are the most severe, and a run without a run_id gets a
unique RAG id."""

from __future__ import annotations

from typing import Any

import pytest

import conductor.wrapup as wu


def test_top_findings_are_the_most_severe() -> None:
    results = [
        {"repo": "a", "findings": [{"severity": "LOW", "title": "l1"}, {"severity": "medium", "title": "m1"},
                                   {"severity": "LOW", "title": "l2"}]},
        {"repo": "b", "findings": [{"severity": "critical", "title": "c1"}, {"severity": "HIGH", "title": "h1"}]},
    ]
    tops = wu.top_findings_from_worker_results(results)
    assert [(t.title, t.severity) for t in tops] == [("c1", "CRITICAL"), ("h1", "HIGH"), ("m1", "MEDIUM")]


@pytest.mark.parametrize("status", [{}, {"run_id": None}, {"run_id": ""}])
def test_runs_without_a_run_id_get_distinct_rag_ids(status: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def post(docs: list[dict[str, Any]], **_k: Any) -> dict[str, Any]:
        sent.extend(d["id"] for d in docs)
        return {"ingested": True}

    monkeypatch.setattr("auditor.rag_client.post_documents", post)
    wu.ingest_latest_to_rag(dict(status), "body")
    wu.ingest_latest_to_rag(dict(status), "body")
    assert len(set(sent)) == 2
    assert not any(s.endswith("-None") for s in sent)


def test_run_id_names_the_rag_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr("auditor.rag_client.post_documents",
                        lambda docs, **_k: sent.extend(d["id"] for d in docs) or {})
    wu.ingest_latest_to_rag({"run_id": "rest-1"}, "body")
    assert sent == ["auditor-rest-rest-1"]
