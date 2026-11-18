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
    monkeypatch.setattr(ei, "with_retries", lambda func, retries=3, base_delay_s=1.0: func())


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
