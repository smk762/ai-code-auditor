"""Regression tests for audit-pack fixes in semantic/embedding_index.py (the remote endpoint is faked)."""
from __future__ import annotations

from pathlib import Path

import pytest

from auditor.config import EmbedderProviderConfig
from auditor.contracts import CodeUnit
from semantic import embedding_index as ei
from semantic.embedding_index import EmbeddingIndex


def _unit(i: int, body: str) -> CodeUnit:
    return CodeUnit(f"u{i}", "svc", f"svc/m{i}.py", f"f{i}", "function", "python", 1, 3,
                    raw_text=body, raw_text_hash=f"h{i}")


def _remote(tmp_path: Path, dim: int = 4) -> EmbeddingIndex:
    cfg = EmbedderProviderConfig(provider="openai-compatible", model="m", dim=dim, endpoint="http://embed.invalid")
    return EmbeddingIndex(path=str(tmp_path / "idx.faiss"), provider_config=cfg)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("auditor.retry.time.sleep", lambda s: None)


def test_the_code_body_is_embedded_not_its_hash(tmp_path: Path, monkeypatch) -> None:
    idx = EmbeddingIndex(path=str(tmp_path / "idx.faiss"),
                         provider_config=EmbedderProviderConfig(provider="local-hash", model="local-hash"))
    seen: list[str] = []
    monkeypatch.setattr(idx, "_embed_text", lambda text: seen.append(text) or [1.0] + [0.0] * (idx.dim - 1))
    idx.rebuild([_unit(1, "def f1():\n    return verify_jwt(token)\n")])
    assert "verify_jwt(token)" in seen[0] and "svc/m1.py" in seen[0]
    assert idx.records[0]["raw_text_hash"] == "h1"


def test_a_remote_outage_fails_instead_of_mixing_hash_vectors(tmp_path: Path, monkeypatch) -> None:
    idx = _remote(tmp_path)
    replies = iter([[0.1, 0.2, 0.3, 0.4], []])  # second call: endpoint down
    monkeypatch.setattr(idx, "_remote_openai_embedding", lambda text: next(replies))
    with pytest.raises(RuntimeError, match="returned no vector"):
        idx.rebuild([_unit(1, "a"), _unit(2, "b")])


def test_remote_vectors_are_fitted_to_the_index_dim(tmp_path: Path, monkeypatch) -> None:
    idx = _remote(tmp_path, dim=4)
    monkeypatch.setattr(idx, "_remote_openai_embedding", lambda text: [3.0, 4.0, 0.0, 0.0, 9.0, 9.0])
    vec = idx._embed_text("x")
    assert vec == pytest.approx([0.6, 0.8, 0.0, 0.0])  # truncated, then unit-normalised
    monkeypatch.setattr(idx, "_remote_openai_embedding", lambda text: [1.0, 0.0])
    with pytest.raises(ValueError, match="returned 2 dims"):
        idx._embed_text("x")


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code, self._body = status, body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise ei.requests.HTTPError(f"{self.status_code}", response=self)

    def json(self) -> dict:
        return self._body


def _posts(monkeypatch, replies: list) -> list:
    calls: list = []

    def post(url, **kw):
        calls.append(url)
        r = replies[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ei.requests, "post", post)
    return calls


def test_a_transient_endpoint_failure_is_retried(tmp_path: Path, monkeypatch) -> None:
    idx = _remote(tmp_path)
    ok = _Resp(200, {"data": [{"embedding": [3.0, 4.0, 0.0, 0.0]}]})
    calls = _posts(monkeypatch, [ei.requests.ConnectionError("reset"), _Resp(503), ok])
    assert idx._embed_text("x") == pytest.approx([0.6, 0.8, 0.0, 0.0])
    assert len(calls) == 3


def test_a_client_error_is_not_retried_and_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    idx = _remote(tmp_path)
    calls = _posts(monkeypatch, [_Resp(401)])
    with pytest.raises(RuntimeError, match="returned no vector"):
        idx._embed_text("x")
    assert len(calls) == 1
    _posts(monkeypatch, [ei.requests.ConnectionError("down")])
    assert idx.health_check() == (False, "embedding endpoint unavailable")


class _FaissThatCannotReadJson:
    @staticmethod
    def read_index(path: str):
        raise RuntimeError("Error in faiss::read_index: not a faiss index")


def test_a_json_index_is_searchable_where_faiss_is_installed(tmp_path: Path, monkeypatch) -> None:
    local = EmbedderProviderConfig(provider="local-hash", model="local-hash")
    monkeypatch.setattr(ei, "faiss", None)
    EmbeddingIndex(path=str(tmp_path / "idx.faiss"), provider_config=local).rebuild([_unit(1, "a"), _unit(2, "b")])
    monkeypatch.setattr(ei, "faiss", _FaissThatCannotReadJson)
    monkeypatch.setattr(ei, "np", object())
    hits = EmbeddingIndex(path=str(tmp_path / "idx.faiss"), provider_config=local).search("svc:svc/m1.py:f1\na")
    assert {h["id"] for h in hits} == {"u1", "u2"}


def test_json_vectors_left_in_the_index_path_by_an_older_build_still_search(tmp_path: Path, monkeypatch) -> None:
    local = EmbedderProviderConfig(provider="local-hash", model="local-hash", dim=2)
    idx = EmbeddingIndex(path=str(tmp_path / "idx.faiss"), provider_config=local)
    idx.path.write_text("[[1.0, 0.0], [0.0, 1.0]]")
    idx.meta_path.write_text('[{"id": "u1"}, {"id": "u2"}]')
    monkeypatch.setattr(ei, "faiss", _FaissThatCannotReadJson)
    monkeypatch.setattr(ei, "np", object())
    assert sorted(h["id"] for h in idx.search("q", top_k=2)) == ["u1", "u2"]
