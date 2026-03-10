from __future__ import annotations

import hashlib
import json
from pathlib import Path

import requests

from auditor.config import EmbedderProviderConfig, get_secret_from_env, load_embedder_provider_config
from auditor.contracts import CodeUnit
from auditor.retry import with_retries

try:
    import faiss  # type: ignore
    import numpy as np  # type: ignore
except ImportError:
    faiss = None
    np = None


DIM = 64


def _hash_embedding(text: str, dim: int = DIM) -> list[float]:
    raw = hashlib.sha256(text.encode("utf-8")).digest()
    vec = [(raw[i % len(raw)] / 255.0) for i in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class EmbeddingIndex:
    def __init__(self, path: str = "index/code_embeddings.faiss", provider_config: EmbedderProviderConfig | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.path.with_suffix(".meta.json")
        self.records: list[dict] = []
        self._faiss_index = None
        self.provider_config = provider_config or load_embedder_provider_config()
        self.dim = self.provider_config.dim
        self.api_key = get_secret_from_env(self.provider_config.api_key_env)

    def rebuild(self, units: list[CodeUnit]) -> None:
        self.records = []
        vectors: list[list[float]] = []
        for unit in units:
            vec = self._embed_text(f"{unit.repo}:{unit.file_path}:{unit.symbol}:{unit.raw_text_hash}")
            vectors.append(vec)
            self.records.append(
                {
                    "id": unit.id,
                    "repo": unit.repo,
                    "file_path": unit.file_path,
                    "symbol": unit.symbol,
                }
            )

        if faiss is not None and np is not None and vectors:
            arr = np.array(vectors, dtype="float32")
            self._faiss_index = faiss.IndexFlatIP(self.dim)
            self._faiss_index.add(arr)
            faiss.write_index(self._faiss_index, str(self.path))
        else:
            self.path.write_text(json.dumps(vectors), encoding="utf-8")

        self.meta_path.write_text(json.dumps(self.records, indent=2), encoding="utf-8")

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self.meta_path.exists():
            return []
        records = json.loads(self.meta_path.read_text(encoding="utf-8"))
        if not records:
            return []

        q_vec = self._embed_text(query)
        if faiss is not None and np is not None and self.path.exists():
            index = faiss.read_index(str(self.path))
            q_arr = np.array([q_vec], dtype="float32")
            _, idxs = index.search(q_arr, min(top_k, len(records)))
            return [records[i] for i in idxs[0] if 0 <= i < len(records)]

        vectors = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        scored = []
        for i, vec in enumerate(vectors):
            score = sum(a * b for a, b in zip(q_vec, vec))
            scored.append((score, i))
        scored.sort(reverse=True)
        return [records[i] for _, i in scored[:top_k]]

    def _embed_text(self, text: str) -> list[float]:
        provider = self.provider_config.provider.lower()
        if provider == "openai-compatible" and self.provider_config.endpoint:
            remote = with_retries(lambda: self._remote_openai_embedding(text), retries=3, base_delay_s=1.0)
            if remote:
                return remote
        return _hash_embedding(text, dim=self.dim)

    def _remote_openai_embedding(self, text: str) -> list[float]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.provider_config.model, "input": text}
        try:
            resp = requests.post(self.provider_config.endpoint, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", [])
            if data and isinstance(data[0], dict):
                emb = data[0].get("embedding", [])
                if emb and isinstance(emb, list):
                    return [float(v) for v in emb[: self.dim]]
        except requests.RequestException:
            return []
        return []

    def health_check(self) -> tuple[bool, str]:
        provider = self.provider_config.provider.lower()
        if provider != "openai-compatible":
            return True, "local embedder"
        sample = self._remote_openai_embedding("health-check")
        if sample:
            return True, "ok"
        return False, "embedding endpoint unavailable"
