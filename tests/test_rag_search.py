from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from auditor import rag_client

HIT = {"score": 0.57, "doc_id": "wiki::mint::architecture::the-http-server", "chunk_index": 0,
       "title": "argus-mint — Architecture — The HTTP server", "url": "http://audit/ui/wiki/argus-mint/architecture#x",
       "path": None, "source": "ai-code-auditor", "tags": ["kind:wiki", "repo:argus-mint"],
       "text": "`Settings.from_env` reads the environment [app.py:L1-L9]."}


class _Resp:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, data, headers, timeout):
        self.calls.append((url, json.loads(data), headers))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("RAG_CHAT_URL", "RAG_CHAT_API_KEY", "AI_AUDIT_RAG_INGEST_URL", "RAG_INGEST_URL"):
        monkeypatch.delenv(k, raising=False)


def test_chat_url_resolution(monkeypatch) -> None:
    assert rag_client.chat_url() == "http://127.0.0.1:9150"
    monkeypatch.setenv("AI_AUDIT_RAG_INGEST_URL", "http://10.0.0.5:9050/ingest")
    assert rag_client.chat_url() == "http://10.0.0.5:9150"
    monkeypatch.setenv("RAG_CHAT_URL", "http://chat.test:1234/")
    assert rag_client.chat_url() == "http://chat.test:1234"


def test_search_sends_filters_and_key(monkeypatch) -> None:
    monkeypatch.setenv("RAG_CHAT_URL", "http://chat.test")
    s = _Session(_Resp(200, {"collection": "audit_docs", "tags": ["repo:argus-mint", "kind:wiki"], "hits": [HIT]}))
    res = rag_client.search("  how is it configured ", repo="argus-mint", kind="wiki", top_k=50, session=s)
    assert res["hits"][0]["title"].startswith("argus-mint")
    url, body, headers = s.calls[0]
    assert url == "http://chat.test/search"
    assert body == {"query": "how is it configured", "top_k": 20, "tags": [], "repo": "argus-mint", "kind": "wiki"}
    assert "Authorization" not in headers
    monkeypatch.setenv("RAG_CHAT_API_KEY", "k.1")
    rag_client.search("q", session=s)
    assert s.calls[1][2]["Authorization"] == "Bearer k.1" and "repo" not in s.calls[1][1]


@pytest.mark.parametrize("resp, needle", [
    (_Resp(404, text="Not Found"), "predates POST /search"),
    (_Resp(401, {"detail": "no"}), "RAG_CHAT_API_KEY"),
    (_Resp(502, {"detail": "Search failed: collection not found"}), "collection not found"),
    (_Resp(500, None, "boom"), "boom"),
    (ConnectionError("refused"), "unreachable"),
])
def test_search_errors(resp, needle) -> None:
    with pytest.raises(RuntimeError, match=needle):
        rag_client.search("q", session=_Session(resp))
    with pytest.raises(ValueError):
        rag_client.search("  ", session=_Session(resp))


def test_search_endpoint(client: TestClient, monkeypatch) -> None:
    seen = {}

    def fake(q, repo=None, kind=None, top_k=5):
        seen.update(q=q, repo=repo, kind=kind, top_k=top_k)
        return {"collection": "audit_docs", "tags": [f"repo:{repo}"], "hits": [HIT]}

    monkeypatch.setattr(rag_client, "search", fake)
    r = client.get("/audit/search", params={"q": "settings", "repo": "argus-mint", "top_k": 3})
    assert r.status_code == 200 and r.json()["hits"][0]["doc_id"] == HIT["doc_id"]
    assert seen == {"q": "settings", "repo": "argus-mint", "kind": None, "top_k": 3}
    assert client.get("/audit/search", params={"q": ""}).status_code == 422
    assert client.get("/audit/search", params={"q": "x", "top_k": 21}).status_code == 422

    def down(*a, **k):
        raise RuntimeError("rag-chat unreachable")

    monkeypatch.setattr(rag_client, "search", down)
    r = client.get("/audit/search", params={"q": "x"})
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_cli_search_prints_hits(monkeypatch, capsys) -> None:
    import sys

    import cli

    monkeypatch.setattr(rag_client, "search", lambda q, **k: {"collection": "audit_docs", "tags": ["kind:wiki"], "hits": [HIT]})
    monkeypatch.setattr(sys, "argv", ["ai-audit", "search", "settings", "--kind", "wiki"])
    cli.main()
    out = capsys.readouterr().out
    assert "1 hit(s) from audit_docs (kind:wiki)" in out and "The HTTP server" in out and "http://audit/ui/wiki" in out

    monkeypatch.setattr(rag_client, "search", lambda q, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(SystemExit, match="search failed: down"):
        cli.main()
