"""Regression tests for wiki/rag.py audit packs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki import rag


class _Resp:
    def __init__(self, body):
        self.status_code, self._body, self.text = 200, body, json.dumps(body)

    def json(self):
        return self._body


class FakeIngest:
    def __init__(self):
        self.ingested: list[dict] = []
        self.deleted: list[str] = []

    def post(self, url, data, headers, timeout):
        body = json.loads(data)
        if url.endswith("/delete"):
            self.deleted += body["doc_ids"]
            return _Resp({"ok": True, "deleted": len(body["doc_ids"])})
        self.ingested.append(body)
        return _Resp({"ok": True})


@pytest.fixture
def fake(monkeypatch) -> FakeIngest:
    import requests

    f = FakeIngest()
    monkeypatch.setattr(requests, "post", f.post)
    monkeypatch.setenv("INGEST_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("AI_AUDIT_RAG_INGEST_URL", "http://ingest.test:9050/ingest")
    monkeypatch.setattr("conductor.publish.public_base_url", lambda: "http://audit")
    return f


def _wiki(tmp_path: Path, pages: list, files: dict[str, bytes]) -> Path:
    d = tmp_path / "wiki" / "svc"
    d.mkdir(parents=True, exist_ok=True)
    (d / "INDEX.json").write_text(json.dumps({"pages": pages}))
    for name, data in files.items():
        (d / name).write_bytes(data)
    return d


def test_malformed_index_entries_skip_only_that_page(tmp_path: Path) -> None:
    _wiki(tmp_path, ["junk", {"slug": "architecture"}, {"slug": "interfaces", "file": "interfaces.md"},
                     {"slug": "overview", "file": "overview.md"}],
          {"interfaces.md": b"# I\n\n## Bad \xff bytes\n", "overview.md": b"# O\n\n## Setup\nx\n"})
    ids = [d["id"] for d in rag.documents_for_repo(tmp_path, "svc")]
    assert ids == ["wiki::svc::overview::introduction", "wiki::svc::overview::setup"]


def test_index_that_is_not_an_object_yields_nothing(tmp_path: Path) -> None:
    d = _wiki(tmp_path, [], {})
    (d / "INDEX.json").write_text("[1, 2]")
    assert rag.documents_for_repo(tmp_path, "svc") == []


def test_section_ids_never_collide(tmp_path: Path) -> None:
    _wiki(tmp_path, [{"slug": "overview", "file": "overview.md"}],
          {"overview.md": b"## Foo\na\n\n## Foo\nb\n\n## Foo 1\nc\n"})
    ids = [d["id"].rsplit("::", 1)[1] for d in rag.documents_for_repo(tmp_path, "svc")]
    assert len(ids) == len(set(ids)) == 3


def test_requested_pages_that_vanished_are_retired(fake, tmp_path: Path) -> None:
    d = _wiki(tmp_path, [{"slug": "overview", "file": "overview.md"}, {"slug": "interfaces", "file": "interfaces.md"}],
              {"overview.md": b"# O\n\n## Setup\nx\n", "interfaces.md": b"# I\n\n## CLI\nz\n"})
    assert rag.ingest(tmp_path)["count"] == 4
    (d / "INDEX.json").write_text(json.dumps({"pages": [{"slug": "interfaces", "file": "interfaces.md"}]}))
    (d / "overview.md").unlink()
    r = rag.ingest(tmp_path, {"svc": {"overview"}})  # a refresh of a page that is now gone
    assert r["retired"] == 2
    assert sorted(fake.deleted) == ["wiki::svc::overview::introduction", "wiki::svc::overview::setup"]


def test_unreadable_index_retires_nothing(fake, tmp_path: Path) -> None:
    d = _wiki(tmp_path, [{"slug": "overview", "file": "overview.md"}], {"overview.md": b"# O\n\n## Setup\nx\n"})
    rag.ingest(tmp_path)
    (d / "INDEX.json").write_text("{broken")
    rag.ingest(tmp_path, {"svc": {"overview"}})
    rag.ingest(tmp_path)
    assert fake.deleted == []
