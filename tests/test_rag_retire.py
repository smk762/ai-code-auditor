from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor import rag_client


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, json.dumps(body or {})

    def json(self):
        return self._body


class FakeIngest:
    """Records /ingest and /delete calls; can be told to fail either."""

    def __init__(self):
        self.ingested: list[dict] = []
        self.deleted: list[dict] = []
        self.fail_delete = self.fail_ingest = None

    def post(self, url, data, headers, timeout):
        body = json.loads(data)
        if url.endswith("/delete"):
            if self.fail_delete:
                return _Resp(self.fail_delete, {"detail": "nope"})
            self.deleted.append(body)
            return _Resp(200, {"ok": True, "deleted": 2 * len(body["doc_ids"])})
        if self.fail_ingest:
            return _Resp(self.fail_ingest, {"detail": "down"})
        self.ingested.append(body)
        return _Resp(200, {"ok": True})

    @property
    def deleted_ids(self):
        return sorted(i for b in self.deleted for i in b["doc_ids"])


@pytest.fixture
def fake(monkeypatch) -> FakeIngest:
    import requests

    f = FakeIngest()
    monkeypatch.setattr(requests, "post", f.post)
    monkeypatch.setenv("INGEST_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("AI_AUDIT_RAG_INGEST_URL", "http://ingest.test:9050/ingest")
    return f


def _ledger() -> dict:
    import os

    p = Path(os.environ["AI_AUDIT_RAG_LEDGER"])
    return json.loads(p.read_text()) if p.is_file() else {}


def test_delete_url_and_documents(fake) -> None:
    assert rag_client.delete_url() == "http://ingest.test:9050/delete"
    r = rag_client.delete_documents(["a", "a", "", "b"])
    assert r == {"deleted": True, "count": 2, "points": 4}
    assert fake.deleted == [{"doc_ids": ["a", "b"], "source": "ai-code-auditor", "collection": "audit_docs"}]
    assert rag_client.delete_documents([]) == {"deleted": True, "count": 0, "points": 0}
    fake.fail_delete = 404
    r = rag_client.delete_documents(["c"])
    assert r["deleted"] is False and "agent-composer#26" in r["error"]


def test_sync_retires_only_what_the_send_replaced(fake) -> None:
    docs = lambda *ids: [{"id": i, "text": i} for i in ids]  # noqa: E731
    r = rag_client.sync_documents(None, "s", docs("a", "b", "c"), tags=["t"])
    assert r["retired"] == 0 and _ledger() == {"s": ["a", "b", "c"]}
    r = rag_client.sync_documents(None, "s", docs("a", "d"), tags=["t"], considered=lambda i: i != "c")
    assert r["retired"] == 1 and fake.deleted_ids == ["b"]  # c is outside the refreshed part
    assert _ledger() == {"s": ["a", "c", "d"]}


def test_failed_delete_is_retried_and_failed_send_retires_nothing(fake) -> None:
    rag_client.sync_documents(None, "s", [{"id": "a", "text": "a"}, {"id": "b", "text": "b"}], tags=[])
    fake.fail_delete = 502
    r = rag_client.sync_documents(None, "s", [{"id": "a", "text": "a"}], tags=[])
    assert r["retired"] == 0 and "nope" in r["retire_error"] and "b" in _ledger()["s"]
    fake.fail_delete = None
    fake.fail_ingest = 503
    r = rag_client.sync_documents(None, "s", [], tags=[])
    assert fake.deleted == []
    fake.fail_ingest = None
    rag_client.sync_documents(None, "s", [{"id": "a", "text": "a"}], tags=[])
    assert fake.deleted_ids == ["b"] and _ledger()["s"] == ["a"]


def test_wiki_ingest_retires_removed_sections(fake, tmp_path, monkeypatch) -> None:
    from wiki import rag

    monkeypatch.setattr("conductor.publish.public_base_url", lambda: "http://audit")
    d = tmp_path / "wiki" / "svc"
    d.mkdir(parents=True)
    (d / "INDEX.json").write_text(json.dumps({"pages": [
        {"slug": "overview", "title": "Overview", "file": "overview.md"},
        {"slug": "interfaces", "title": "Interfaces", "file": "interfaces.md"}]}))
    (d / "overview.md").write_text("# O\n\nintro\n\n## Setup\nx\n\n## Old name\ny\n")
    (d / "interfaces.md").write_text("# I\n\n## CLI\nz\n")
    assert rag.ingest(tmp_path)["count"] == 5  # each page's intro is a section too
    (d / "overview.md").write_text("# O\n\nintro\n\n## Setup\nx\n\n## New name\ny\n")
    (d / "interfaces.md").write_text("# I\n\n## HTTP\nz\n")
    r = rag.ingest(tmp_path, {"svc": {"overview"}})  # a refresh that rebuilt only overview
    assert r["retired"] == 1 and fake.deleted_ids == ["wiki::svc::overview::old-name"]
    r = rag.ingest(tmp_path)
    assert r["retired"] == 1 and fake.deleted_ids == ["wiki::svc::interfaces::cli", "wiki::svc::overview::old-name"]
    ledger = json.loads((tmp_path / "rag_ingested.json").read_text())  # explicit output_dir wins over the env
    assert sorted(ledger["wiki:svc"]) == ["wiki::svc::interfaces::http", "wiki::svc::interfaces::introduction",
                                              "wiki::svc::overview::introduction", "wiki::svc::overview::new-name",
                                              "wiki::svc::overview::setup"]


def test_packs_closed_are_retired_and_full_sync_drops_vanished(fake) -> None:
    from auditor import agent_pack
    from tests.test_agent_pack import _mk_pack

    a, b, c = _mk_pack("pack-a", status="open"), _mk_pack("pack-b", status="claimed"), _mk_pack("pack-c")
    r = agent_pack.ingest_packs_to_rag([a, b, c])
    assert r["count"] == 3 and r["retired"] == 0
    b.status = "resolved"
    r = agent_pack.ingest_packs_to_rag([b])  # the status endpoint's single-pack refresh
    assert r["count"] == 0 and fake.deleted_ids == ["agent-pack::pack-b"]
    assert "agent-pack::pack-b" not in _ledger()["packs"]
    r = agent_pack.ingest_packs_to_rag([a], full=True)  # pack-c no longer on disk
    assert r["retired"] == 1 and fake.deleted_ids == ["agent-pack::pack-b", "agent-pack::pack-c"]
    assert _ledger()["packs"] == ["agent-pack::pack-a"]
    # a partial send never retires other packs
    agent_pack.ingest_packs_to_rag([c])
    agent_pack.ingest_packs_to_rag([a])
    assert _ledger()["packs"] == ["agent-pack::pack-a", "agent-pack::pack-c"]
