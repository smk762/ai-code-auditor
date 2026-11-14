"""Post documents to agent-composer's rag-ingest, speaking its current protocol.

rag-ingest (``POST /ingest``) expects:
- body ``{"docs": [...], "source": str, "tags": [str]}``. Each doc keeps only ``id``/``doc_id``,
  ``title``, ``url``, ``path`` and ``source`` besides its text, so anything else worth filtering on
  goes into ``tags`` (``kind:wiki``, ``repo:argus-cortex``).
- headers ``X-Timestamp``, ``X-Nonce`` (single use, replay-protected) and ``X-Signature`` =
  HMAC-SHA256 keyed by sha256(INGEST_SHARED_SECRET) over ``ts.nonce.sha256hex(body)``.

The collection is chosen by the service (its ``QDRANT_COLLECTION``), not by the request: point
``AI_AUDIT_RAG_INGEST_URL`` at the instance that writes the collection you want. A
``collection`` field is still sent for services that accept one; others ignore it.

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
