"""The embedding index refuses to answer from vectors it can't compare with the query."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auditor.config import EmbedderProviderConfig
from auditor.contracts import CodeUnit
from semantic import embedding_index as ei
from semantic.embedding_index import EmbeddingIndex


def _unit(i: int) -> CodeUnit:
    return CodeUnit(f"u{i}", "svc", f"svc/m{i}.py", f"f{i}", "function", "python", 1, 3,
                    raw_text=f"body {i}", raw_text_hash=f"h{i}")


def _local(dim: int = 64) -> EmbedderProviderConfig:
    return EmbedderProviderConfig(provider="local-hash", model="local-hash", dim=dim)


@pytest.fixture(autouse=True)
def _no_faiss(monkeypatch):
    monkeypatch.setattr(ei, "faiss", None)
    monkeypatch.setattr(ei, "np", None)


def test_a_dim_change_since_the_build_is_reported_stale(tmp_path: Path) -> None:
    path = str(tmp_path / "idx.faiss")
    EmbeddingIndex(path=path, provider_config=_local(64)).rebuild([_unit(1), _unit(2)])
    assert EmbeddingIndex(path=path, provider_config=_local(64)).search("q")  # same embedder: fine
    with pytest.raises(ei.StaleIndexError, match="rebuild"):
        EmbeddingIndex(path=path, provider_config=_local(32)).search("q")


def test_a_same_dim_embedder_swap_is_reported_stale(tmp_path: Path, monkeypatch) -> None:
    path = str(tmp_path / "idx.faiss")
    EmbeddingIndex(path=path, provider_config=_local(4)).rebuild([_unit(1)])
    remote = EmbeddingIndex(path=path, provider_config=EmbedderProviderConfig(
        provider="openai-compatible", model="m", dim=4, endpoint="http://embed.invalid"))
    monkeypatch.setattr(remote, "_remote_openai_embedding", lambda text: [1.0, 0.0, 0.0, 0.0])
    with pytest.raises(ei.StaleIndexError):
        remote.search("q")


def test_a_faiss_index_where_faiss_is_missing_is_an_error_not_no_matches(tmp_path: Path) -> None:
    idx = EmbeddingIndex(path=str(tmp_path / "idx.faiss"), provider_config=_local())
    idx.path.write_bytes(b"IxFI\x00\x00\x80\xff binary faiss index")  # what a faiss build leaves
    idx.meta_path.write_text('[{"id": "u1"}, {"id": "u2"}]')
    with pytest.raises(ei.StaleIndexError, match="faiss"):
        idx.search("q")


def test_a_faiss_index_of_another_width_is_refused_before_searching(tmp_path: Path, monkeypatch) -> None:
    searched: list = []
    index = SimpleNamespace(d=32, search=lambda q, k: searched.append(q))
    monkeypatch.setattr(ei, "faiss", SimpleNamespace(read_index=lambda p: index))
    monkeypatch.setattr(ei, "np", SimpleNamespace(array=lambda v, dtype=None: v))
    idx = EmbeddingIndex(path=str(tmp_path / "idx.faiss"), provider_config=_local(64))
    idx.path.write_bytes(b"faiss")
    idx.meta_path.write_text('[{"id": "u1"}]')  # an older build: no embedder record
    with pytest.raises(ei.StaleIndexError):
        idx.search("q")
    assert searched == []
