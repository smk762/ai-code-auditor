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
MAX_EMBED_CHARS = 8000  # code body cap per unit (embedding endpoints have an input token limit)


def _unit_text(unit: CodeUnit) -> str:
    """What is embedded for a unit: where it lives, then its code (the hash is metadata, not meaning)."""
    return f"{unit.repo}:{unit.file_path}:{unit.symbol}\n{(unit.raw_text or '')[:MAX_EMBED_CHARS]}"


def _transient(exc: Exception) -> bool:
    """Worth another attempt: no response, a timeout, a 429 or a 5xx (a 4xx would repeat itself)."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return True
    return resp.status_code == 429 or resp.status_code >= 500


def _hash_embedding(text: str, dim: int = DIM) -> list[float]:
    raw = hashlib.sha256(text.encode("utf-8")).digest()
    vec = [(raw[i % len(raw)] / 255.0) for i in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class StaleIndexError(RuntimeError):
    """The stored index can't answer this query as configured: rebuild it."""


class EmbeddingIndex:
    def __init__(self, path: str = "index/code_embeddings.faiss", provider_config: EmbedderProviderConfig | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.path.with_suffix(".meta.json")
        # Vectors stored without faiss go to their own file, so an index built where faiss is missing
        # is still readable where it is installed (and the other way round).
        self.vectors_path = self.path.with_suffix(".vectors.json")
        # Which embedder wrote the vectors: a query embedded by another one is in a different space.
        self.embedder_path = self.path.with_suffix(".embedder.json")
        self.records: list[dict] = []
        self._faiss_index = None
        self.provider_config = provider_config or load_embedder_provider_config()
        self.dim = self.provider_config.dim
        self.api_key = get_secret_from_env(self.provider_config.api_key_env)

    def rebuild(self, units: list[CodeUnit]) -> None:
        self.records = []
        vectors: list[list[float]] = []
        for unit in units:
            vec = self._embed_text(_unit_text(unit))
            vectors.append(vec)
            self.records.append(
                {
                    "id": unit.id,
                    "repo": unit.repo,
                    "file_path": unit.file_path,
                    "symbol": unit.symbol,
                    "raw_text_hash": unit.raw_text_hash,  # for staleness checks
                }
            )

        if faiss is not None and np is not None and vectors:
            arr = np.array(vectors, dtype="float32")
            self._faiss_index = faiss.IndexFlatIP(self.dim)
            self._faiss_index.add(arr)
            faiss.write_index(self._faiss_index, str(self.path))
            self.vectors_path.unlink(missing_ok=True)
        else:
            self.vectors_path.write_text(json.dumps(vectors), encoding="utf-8")
            self.path.unlink(missing_ok=True)  # a stale faiss index would describe other records

        self.meta_path.write_text(json.dumps(self.records, indent=2), encoding="utf-8")
        self.embedder_path.write_text(json.dumps(self._embedder_identity()), encoding="utf-8")

    def _embedder_identity(self) -> dict:
        cfg = self.provider_config
        return {"provider": cfg.provider.lower(), "model": cfg.model, "dim": self.dim}

    def _check_embedder(self) -> None:
        """Refuse an index another embedder built (indexes from before this record are trusted)."""
        if not self.embedder_path.exists():
            return
        try:
            built = json.loads(self.embedder_path.read_text(encoding="utf-8"))
        except ValueError:
            built = None
        if built != self._embedder_identity():
            raise StaleIndexError(f"index {self.path} was built by embedder {built}, but "
                                  f"{self._embedder_identity()} is configured: index stale, rebuild required")

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self.meta_path.exists():
            return []
        records = json.loads(self.meta_path.read_text(encoding="utf-8"))
        if not records:
            return []

        self._check_embedder()
        q_vec = self._embed_text(query)
        if faiss is not None and np is not None and self.path.exists() and not self.vectors_path.exists():
            try:
                index = faiss.read_index(str(self.path))
            except Exception:  # noqa: BLE001 — e.g. JSON vectors written there by an older build
                index = None
            if index is not None:
                if index.d != len(q_vec):
                    raise StaleIndexError(f"index {self.path} holds {index.d}-dim vectors, but queries are "
                                          f"{len(q_vec)}-dim: index stale, rebuild required")
                q_arr = np.array([q_vec], dtype="float32")
                _, idxs = index.search(q_arr, min(top_k, len(records)))
                return [records[i] for i in idxs[0] if 0 <= i < len(records)]

        vectors = self._json_vectors()
        if not vectors:
            # records but nothing readable, e.g. a faiss index where faiss isn't installed: not "no matches"
            raise StaleIndexError(f"index {self.path} has {len(records)} records but no vectors this environment "
                                  "can read (built with faiss?): install faiss or rebuild the index")
        if any(len(vec) != len(q_vec) for vec in vectors):
            raise StaleIndexError(f"index {self.path} holds vectors of another width than the {len(q_vec)}-dim "
                                  "query: index stale, rebuild required")
        scored = []
        for i, vec in enumerate(vectors):
            score = sum(a * b for a, b in zip(q_vec, vec))
            scored.append((score, i))
        scored.sort(reverse=True)
        return [records[i] for _, i in scored[:top_k]]

    def _json_vectors(self) -> list[list[float]]:
        """Vectors stored as JSON: the vectors file, else (older builds) JSON in the index path."""
        for path in (self.vectors_path, self.path):
            if not path.exists():
                continue
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue  # a binary faiss index this environment cannot read
        return []

    def _embed_text(self, text: str) -> list[float]:
        provider = self.provider_config.provider.lower()
        if provider == "openai-compatible" and self.provider_config.endpoint:
            try:
                remote = with_retries(lambda: self._remote_openai_embedding(text), retries=3, base_delay_s=1.0,
                                      retry_if=_transient)
                error = ""
            except requests.RequestException as exc:  # retries exhausted, or a non-transient error
                remote, error = [], f": {exc}"
            if not remote:
                # never fall back to hash vectors here: they live in a different space, so a mixed
                # index (or a hash query against remote vectors) returns arbitrary results
                raise RuntimeError(f"embedding endpoint {self.provider_config.endpoint} returned no vector"
                                   f"{error} (remote embedder configured; not falling back to local hashes)")
            return self._fit(remote)
        return _hash_embedding(text, dim=self.dim)

    def _fit(self, emb: list[float]) -> list[float]:
        """The vector at the configured dim, unit-normalised for inner-product search. A longer one is
        truncated (valid for Matryoshka models such as text-embedding-3) then renormalised; a shorter
        one cannot fit the index."""
        if len(emb) < self.dim:
            raise ValueError(f"embedder {self.provider_config.model!r} returned {len(emb)} dims, but the index "
                             f"is configured for {self.dim} (set AI_AUDIT_EMBEDDER_DIM to match)")
        vec = emb[: self.dim]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def _remote_openai_embedding(self, text: str) -> list[float]:
        """One embedding request. HTTP and network errors are raised, so the caller can retry them."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.provider_config.model, "input": text}
        resp = requests.post(self.provider_config.endpoint, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", []) if isinstance(body, dict) else []
        if data and isinstance(data[0], dict):
            emb = data[0].get("embedding", [])
            if emb and isinstance(emb, list):
                return [float(v) for v in emb]
        return []

    def health_check(self) -> tuple[bool, str]:
        provider = self.provider_config.provider.lower()
        if provider != "openai-compatible":
            return True, "local embedder"
        try:
            sample = self._remote_openai_embedding("health-check")
        except requests.RequestException:
            sample = []
        if sample and len(sample) < self.dim:
            return False, f"embedder returns {len(sample)} dims, index configured for {self.dim}"
        if sample:
            return True, "ok"
        return False, "embedding endpoint unavailable"
