"""rag_client: an IPv6 rag-ingest host gives a valid rag-chat URL, and undecodable ledger bytes
are moved aside like any other corrupt ledger."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from auditor import rag_client


@pytest.mark.parametrize("ingest, expected", [
    ("http://[::1]:9050/ingest", "http://[::1]:9150"),
    ("http://[fd00::1:2]:9050/ingest", "http://[fd00::1:2]:9150"),
    ("http://10.0.0.5:9050/ingest", "http://10.0.0.5:9150"),
    ("http://rag.local:9050/ingest", "http://rag.local:9150"),
])
def test_chat_url_from_ingest_host(monkeypatch: pytest.MonkeyPatch, ingest: str, expected: str) -> None:
    monkeypatch.delenv("RAG_CHAT_URL", raising=False)
    monkeypatch.delenv("RAG_INGEST_URL", raising=False)
    monkeypatch.setenv("AI_AUDIT_RAG_INGEST_URL", ingest)
    url = rag_client.chat_url()
    assert url == expected
    assert urlsplit(url).port == 9150


def test_non_utf8_ledger_is_moved_aside_and_updates_go_on(tmp_path: Path) -> None:
    ledger = tmp_path / rag_client.LEDGER_FILE
    garbage = b'{"packs": ["\xff\xfe'
    ledger.write_bytes(garbage)
    rag_client.update_ledger(tmp_path, "packs", add={"a"})
    aside = list(tmp_path.glob(f"{rag_client.LEDGER_FILE}.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == garbage
    assert json.loads(ledger.read_text(encoding="utf-8")) == {"packs": ["a"]}
    rag_client.update_ledger(tmp_path, "packs", add={"b"})
    assert json.loads(ledger.read_text(encoding="utf-8")) == {"packs": ["a", "b"]}
