"""The embedding index records the embedder it really used, and an interrupted rebuild is refused."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor.config import EmbedderProviderConfig
from auditor.contracts import CodeUnit
from semantic import embedding_index as ei
from semantic.embedding_index import EmbeddingIndex


def _unit(i: int) -> CodeUnit:
    return CodeUnit(f"u{i}", "svc", f"svc/m{i}.py", f"f{i}", "function", "python", 1, 3,
                    raw_text=f"body {i}", raw_text_hash=f"h{i}")


def _local(dim: int = 4) -> EmbedderProviderConfig:
    return EmbedderProviderConfig(provider="local-hash", model="local-hash", dim=dim)


def _remote(endpoint: str, dim: int = 4) -> EmbedderProviderConfig:
    return EmbedderProviderConfig(provider="openai-compatible", model="m", dim=dim, endpoint=endpoint)


@pytest.fixture(autouse=True)
def _no_faiss(monkeypatch):
    monkeypatch.setattr(ei, "faiss", None)
    monkeypatch.setattr(ei, "np", None)


def test_hash_vectors_built_without_an_endpoint_are_refused_once_one_is_set(tmp_path: Path, monkeypatch) -> None:
    path = str(tmp_path / "idx.faiss")
    EmbeddingIndex(path=path, provider_config=_remote("")).rebuild([_unit(1), _unit(2)])
    remote = EmbeddingIndex(path=path, provider_config=_remote("http://embed.invalid"))
    monkeypatch.setattr(remote, "_remote_openai_embedding", lambda text: [1.0, 0.0, 0.0, 0.0])
    with pytest.raises(ei.StaleIndexError):
        remote.search("q")


def test_no_endpoint_reports_the_hash_fallback_in_the_health_check(tmp_path: Path) -> None:
    ok, msg = EmbeddingIndex(path=str(tmp_path / "idx.faiss"), provider_config=_remote("")).health_check()
    assert not ok and "hash" in msg


def test_a_rebuild_interrupted_after_the_vectors_is_refused(tmp_path: Path, monkeypatch) -> None:
    path = str(tmp_path / "idx.faiss")
    EmbeddingIndex(path=path, provider_config=_local()).rebuild([_unit(1), _unit(2)])
    real_dumps = json.dumps

    def crash_on_records(obj, *a, **k):
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            raise MemoryError("killed while writing the records")
        return real_dumps(obj, *a, **k)

    monkeypatch.setattr(ei.json, "dumps", crash_on_records)
    with pytest.raises(MemoryError):
        EmbeddingIndex(path=path, provider_config=_local()).rebuild([_unit(i) for i in range(1, 6)])
    monkeypatch.setattr(ei.json, "dumps", real_dumps)

    with pytest.raises(ei.StaleIndexError):
        EmbeddingIndex(path=path, provider_config=_local()).search("q", top_k=5)


def test_more_vectors_than_records_is_stale_not_an_index_error(tmp_path: Path) -> None:
    idx = EmbeddingIndex(path=str(tmp_path / "idx.faiss"), provider_config=_local())
    idx.vectors_path.write_text(json.dumps([[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0]]))
    idx.meta_path.write_text('[{"id": "u1"}]')  # an older build: no embedder record
    with pytest.raises(ei.StaleIndexError):
        idx.search("q", top_k=3)
