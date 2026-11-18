"""Regression tests for auditor/rag_client.py (delete URL, search decoding, ledger safety)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor import rag_client


@pytest.mark.parametrize(
    "ingest", ["http://host:9050/ingest", "http://host:9050/ingest/", "http://host:9050/ingest//"],
)
def test_delete_url_ignores_trailing_slashes(monkeypatch, ingest):
    monkeypatch.setenv("AI_AUDIT_RAG_INGEST_URL", ingest)
    assert rag_client.delete_url() == "http://host:9050/delete"


class _HtmlResp:
    status_code = 200
    text = "<html>captive portal</html>"

    def json(self):
        raise json.JSONDecodeError("Expecting value", self.text, 0)


class _Session:
    def post(self, url, data, headers, timeout):
        return _HtmlResp()


def test_non_json_2xx_search_is_an_upstream_runtime_error():
    with pytest.raises(RuntimeError, match="non-JSON"):
        rag_client.search("q", session=_Session(), url="http://chat.test/search")


@pytest.mark.parametrize("body", ["", "{truncated", "[1, 2]"])
def test_corrupt_ledger_is_moved_aside_not_silently_wiped(tmp_path: Path, body: str):
    ledger = tmp_path / rag_client.LEDGER_FILE
    ledger.write_text(body, encoding="utf-8")
    rag_client.update_ledger(tmp_path, "packs", add={"a"})
    aside = list(tmp_path.glob(f"{rag_client.LEDGER_FILE}.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == body
    assert json.loads(ledger.read_text(encoding="utf-8")) == {"packs": ["a"]}


def test_unreadable_ledger_raises_instead_of_overwriting(tmp_path: Path, monkeypatch):
    ledger = tmp_path / rag_client.LEDGER_FILE
    original = json.dumps({"wiki:x": ["w1"], "packs": ["p1"]})
    ledger.write_text(original, encoding="utf-8")
    real_read_text = Path.read_text

    def flaky_read_text(self, *a, **k):
        if self == ledger:
            raise PermissionError("transient")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    with pytest.raises(OSError):
        rag_client.update_ledger(tmp_path, "packs", add={"p2"})
    monkeypatch.undo()
    assert ledger.read_text(encoding="utf-8") == original
