"""wiki/rag.py: a failed retire of vanished pages marks the run failed (batch-4 pack). No real RAG sends."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_wiki_rag_packs import _Resp, _wiki, fake  # noqa: F401  (fixture)
from wiki import rag


def _vanish_overview(tmp_path: Path) -> None:
    d = _wiki(tmp_path, [{"slug": "overview", "file": "overview.md"}, {"slug": "interfaces", "file": "interfaces.md"}],
              {"overview.md": b"# O\n\n## Setup\nx\n", "interfaces.md": b"# I\n\n## CLI\nz\n"})
    assert rag.ingest(tmp_path)["ingested"]
    (d / "INDEX.json").write_text(json.dumps({"pages": [{"slug": "interfaces", "file": "interfaces.md"}]}))
    (d / "overview.md").unlink()


def test_a_failed_retire_of_vanished_pages_fails_the_run(fake, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    _vanish_overview(tmp_path)
    post = fake.post

    def delete_fails(url, data, headers, timeout):
        if url.endswith("/delete"):
            r = _Resp({"ok": False})
            r.status_code = 503
            return r
        return post(url, data, headers, timeout)

    monkeypatch.setattr("requests.post", delete_fails)
    r = rag.ingest(tmp_path, {"svc": {"overview"}})
    assert r["ingested"] is False and r["repos"]["svc"]["ingested"] is False
    assert r["repos"]["svc"]["retire_error"] is not None

    monkeypatch.setattr("requests.post", post)  # the retry retires them
    r = rag.ingest(tmp_path, {"svc": {"overview"}})
    assert r["ingested"] and r["retired"] == 2


def test_a_missing_secret_on_retire_stops_like_a_skipped_send(fake, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    _vanish_overview(tmp_path)
    (tmp_path / "wiki" / "zz").mkdir()
    (tmp_path / "wiki" / "zz" / "INDEX.json").write_text(json.dumps({"pages": []}))
    monkeypatch.delenv("INGEST_SHARED_SECRET")
    r = rag.ingest(tmp_path, {"svc": {"overview"}, "zz": {"overview"}})
    assert r["ingested"] is False and "INGEST_SHARED_SECRET" in r["error"]
    assert "zz" not in r["repos"]  # no point trying the other repos
