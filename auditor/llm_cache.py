"""Content-addressed cache of per-unit LLM reviews.

A unit is reviewed once per (provider, model, effort, instructions, file, symbol, code) —
unchanged code is never re-sent, so spend scales with churn rather than run frequency.
The repo/file briefings are deliberately NOT part of the key: they change often and would
defeat the cache. Entries expire after AI_AUDIT_LLM_CACHE_TTL_DAYS (default 60) so units
are periodically re-reviewed with fresher context.

Env: AI_AUDIT_LLM_CACHE=0 disables; AI_AUDIT_LLM_CACHE_PATH (default index/llm_review_cache.db).
The DB also tracks in-flight Message Batches so an interrupted run can harvest them later.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from auditor.contracts import CodeUnit

DEFAULT_PATH = "index/llm_review_cache.db"


def review_key(*, provider: str, model: str, effort: str, instructions: str, unit: CodeUnit) -> str:
    material = json.dumps(
        [provider, model, effort, instructions, unit.repo, unit.file_path, unit.symbol, unit.raw_text]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ReviewCache:
    def __init__(self, path: str | Path, ttl_days: float = 60.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_days * 86400 if ttl_days > 0 else None
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                key TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                created_at REAL NOT NULL,
                harvested INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self._conn.commit()

    @classmethod
    def from_env(cls) -> "ReviewCache | None":
        if os.getenv("AI_AUDIT_LLM_CACHE", "1").strip().lower() in {"0", "false", "no", "off"}:
            return None
        return cls(
            os.getenv("AI_AUDIT_LLM_CACHE_PATH", DEFAULT_PATH),
            ttl_days=float(os.getenv("AI_AUDIT_LLM_CACHE_TTL_DAYS", "60")),
        )

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT response, created_at FROM reviews WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        if self.ttl_s is not None and time.time() - row[1] > self.ttl_s:
            return None
        return str(row[0])

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def put(self, key: str, model: str, response: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO reviews (key, model, response, created_at) VALUES (?, ?, ?, ?)",
                (key, model, response, time.time()),
            )
            self._conn.commit()

    # ── Message Batch bookkeeping ────────────────────────────────────────────
    def add_batch(self, batch_id: str, model: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO batches (batch_id, model, created_at) VALUES (?, ?, ?)",
                (batch_id, model, time.time()),
            )
            self._conn.commit()

    def pending_batches(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT batch_id FROM batches WHERE harvested = 0 ORDER BY created_at"
            ).fetchall()
        return [r[0] for r in rows]

    def mark_harvested(self, batch_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE batches SET harvested = 1 WHERE batch_id = ?", (batch_id,))
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            p = self._conn.execute("SELECT COUNT(*) FROM batches WHERE harvested = 0").fetchone()[0]
        return {"reviews": int(n), "pending_batches": int(p)}
