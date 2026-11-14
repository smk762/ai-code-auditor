"""Post documents to agent-composer's rag-ingest, speaking its current protocol.

rag-ingest (``POST /ingest``) expects:
- body ``{"docs": [...], "source": str, "tags": [str]}``. Each doc keeps only ``id``/``doc_id``,
  ``title``, ``url``, ``path`` and ``source`` besides its text, so anything else worth filtering on
  goes into ``tags`` (``kind:wiki``, ``repo:argus-cortex``).
- headers ``X-Timestamp``, ``X-Nonce`` (single use, replay-protected) and ``X-Signature`` =
  HMAC-SHA256 keyed by sha256(INGEST_SHARED_SECRET) over ``ts.nonce.sha256hex(body)``.

Each request names its ``collection`` (``AI_AUDIT_RAG_COLLECTION``, default ``audit_docs``).
rag-ingest accepts its own ``QDRANT_COLLECTION`` (now ``audit_docs`` by default) or one listed in
``INGEST_ALLOWED_COLLECTIONS``, and refuses anything else with 400. ``project_docs`` is deprecated.

Reading goes through rag-chat's ``POST /search`` (``search`` below). That way the embedding
model that wrote the vectors is also the one that embeds the query, and only rag-chat has to
know it.

Before this module, the pack and night-conductor ingests sent ``{"collection", "documents"}``
without a nonce, and every ingest failed with HTTP 422.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
SOURCE = "ai-code-auditor"


def ingest_url() -> str:
    return os.getenv("AI_AUDIT_RAG_INGEST_URL") or os.getenv("RAG_INGEST_URL") or "http://127.0.0.1:9050/ingest"


def sign(secret: str, ts: str, nonce: str, body: bytes) -> str:
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    msg = ts.encode("ascii") + b"." + nonce.encode("ascii") + b"." + hashlib.sha256(body).hexdigest().encode("ascii")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def post_documents(docs: list[dict[str, Any]], *, tags: list[str] | None = None, source: str = SOURCE,
                   batch_size: int = BATCH_SIZE, session: Any = None, url: str | None = None) -> dict[str, Any]:
    """Send docs in batches. Returns {ingested, count, ...}; never raises on HTTP trouble."""
    if not docs:
        return {"ingested": False, "skipped": True, "count": 0}
    secret = os.getenv("INGEST_SHARED_SECRET", "")
    if not secret:
        return {"ingested": False, "skipped": True, "count": 0,
                "error": "INGEST_SHARED_SECRET is not set; rag-ingest rejects unsigned requests"}
    if session is None:
        import requests as session  # noqa: N811 — module with the same .post as a Session

    collection = os.getenv("AI_AUDIT_RAG_COLLECTION", "audit_docs")
    result: dict[str, Any] = {"ingested": True, "skipped": False, "count": 0}
    for start in range(0, len(docs), batch_size):
        batch = docs[start:start + batch_size]
        body = json.dumps({"docs": batch, "source": source, "tags": list(tags or []),
                           "collection": collection}).encode("utf-8")
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        headers = {"Content-Type": "application/json", "X-Timestamp": ts, "X-Nonce": nonce,
                   "X-Signature": sign(secret, ts, nonce, body)}
        try:
            resp = session.post(url or ingest_url(), data=body, headers=headers, timeout=120)
        except Exception as exc:  # noqa: BLE001 — network trouble is reported, not raised
            logger.warning("RAG ingest failed: %s", exc)
            return {**result, "ingested": False, "error": str(exc)[:300]}
        if not 200 <= resp.status_code < 300:
            logger.warning("RAG ingest HTTP %s: %s", resp.status_code, (resp.text or "")[:300])
            return {**result, "ingested": False, "status_code": resp.status_code,
                    "error": (resp.text or "")[:300]}
        result["count"] += len(batch)
        result["status_code"] = resp.status_code
    return result


def chat_url() -> str:
    """rag-chat's base URL: ``RAG_CHAT_URL``, else the rag-ingest host on port 9150, else localhost."""
    explicit = os.getenv("RAG_CHAT_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    from urllib.parse import urlsplit

    ingest = os.getenv("AI_AUDIT_RAG_INGEST_URL") or os.getenv("RAG_INGEST_URL") or ""
    host = urlsplit(ingest).hostname if ingest else None
    return f"http://{host or '127.0.0.1'}:9150"


def search(query: str, *, repo: str | None = None, kind: str | None = None, tags: list[str] | None = None,
           top_k: int = 5, session: Any = None, url: str | None = None, timeout: float = 60) -> dict[str, Any]:
    """Search the audit collection through rag-chat's ``POST /search``.

    Returns rag-chat's ``{collection, tags, hits: [{score, title, url, doc_id, tags, text, ...}]}``.
    Raises ``ValueError`` for an empty query and ``RuntimeError`` (with rag-chat's detail) when
    rag-chat can't be reached or answers with an error.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("empty query")
    if session is None:
        import requests as session  # noqa: N811

    headers = {"Content-Type": "application/json"}
    key = os.getenv("RAG_CHAT_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body: dict[str, Any] = {"query": query, "top_k": max(1, min(int(top_k), 20)), "tags": list(tags or [])}
    if repo:
        body["repo"] = repo
    if kind:
        body["kind"] = kind
    target = url or f"{chat_url()}/search"
    try:
        resp = session.post(target, data=json.dumps(body), headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"rag-chat unreachable at {target}: {exc}") from exc
    if resp.status_code == 404:
        raise RuntimeError(f"{target} returned 404: this rag-chat predates POST /search (agent-composer#25)")
    if resp.status_code == 401:
        raise RuntimeError("rag-chat requires an API key: set RAG_CHAT_API_KEY")
    if not 200 <= resp.status_code < 300:
        try:
            detail = resp.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:300]
        raise RuntimeError(f"rag-chat search HTTP {resp.status_code}: {detail}")
    return resp.json()
