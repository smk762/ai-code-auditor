"""``ai-audit repair --pack-id`` passes the pack through to the rag-chat repair API."""
from __future__ import annotations

import pytest

import cli


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = True):
        yield "event: pack_loaded"
        yield 'data: {"pack_id": "ext-1", "title": "T", "repo": "argus-cortex"}'
        yield "event: warning"
        yield 'data: {"message": "soft"}'


def _capture(monkeypatch) -> dict:
    import requests

    seen: dict = {}

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        seen.update(url=url, payload=json)
        return _FakeResp()

    monkeypatch.setattr(requests, "post", fake_post)
    return seen


def test_pack_id_only_builds_payload(monkeypatch, capsys) -> None:
    seen = _capture(monkeypatch)
    cli._run_repair(
        repo_name="",
        repair_api_url="http://rag.test",
        pack_id="ext-1",
        audit_api_base="http://audit.test:8765",
    )
    payload = seen["payload"]
    assert seen["url"] == "http://rag.test/api/repair/run"
    assert payload["pack_id"] == "ext-1"
    assert payload["audit_api_base"] == "http://audit.test:8765"
    assert payload["repo"] == ""
    assert payload["diff"] == ""
    out = capsys.readouterr().out
    assert "ext-1" in out and "Warning: soft" in out and "Error" not in out


def test_without_pack_id_payload_unchanged(monkeypatch, tmp_path) -> None:
    seen = _capture(monkeypatch)
    diff = tmp_path / "x.diff"
    diff.write_text("--- a\n+++ b\n", encoding="utf-8")
    cli._run_repair(repo_name="argus-cortex", repair_diff_file=str(diff), repair_api_url="http://rag.test")
    assert "pack_id" not in seen["payload"]
    assert "audit_api_base" not in seen["payload"]
    assert seen["payload"]["diff"].startswith("--- a")


def test_requires_a_source_or_pack(monkeypatch) -> None:
    _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli._run_repair(repo_name="argus-cortex")
    with pytest.raises(SystemExit):
        cli._run_repair(repo_name="", repair_stdin=False, repair_compare_branch="main")
