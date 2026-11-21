"""Content-addressed cache of per-unit LLM reviews.

A unit is reviewed once per (provider, model, effort, instructions, file, symbol, code) —
unchanged code is never re-sent, so spend scales with churn rather than run frequency.
The repo/file briefings are deliberately NOT part of the key: they change often and would
defeat the cache.

The code goes into the key normalised (`normalise_code`), so a format-only change (re-indent,
re-wrapped lines, trailing or blank-line whitespace) is still a cache hit. Python is compared
token by token, so string contents and comments still count; other languages line by line.
Keys from before normalisation (`legacy_review_key`) are still read, and a hit is copied under
the new key with its original age, so the switch triggers no rescan. The fallback can go once
every legacy entry has expired (TTL plus jitter, so 2027-02 at the default 60 days).

Entries expire after AI_AUDIT_LLM_CACHE_TTL_DAYS (default 60) so units are periodically
re-reviewed with fresher context. Each key's TTL is spread by ±AI_AUDIT_LLM_CACHE_TTL_JITTER
(default 0.25, so 45-75 days), fixed per key, so a codebase reviewed in one night does not
all expire, and get re-reviewed, on one night.

Env: AI_AUDIT_LLM_CACHE=0 disables; AI_AUDIT_LLM_CACHE_PATH (default index/llm_review_cache.db).
The DB also tracks in-flight Message Batches so an interrupted run can harvest them later.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import textwrap
import threading
import time
import tokenize
from pathlib import Path

from auditor.contracts import CodeUnit

DEFAULT_PATH = "index/llm_review_cache.db"


_WS = re.compile(r"\s+")
_PY_SKIP = {tokenize.NL, tokenize.ENDMARKER, tokenize.ENCODING}


def _normalise_lines(text: str) -> str:
    lines = (_WS.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _normalise_python(text: str) -> str | None:
    """Token stream without layout: indent depth survives, its width and line wrapping don't."""
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(textwrap.dedent(text)).readline):
            if tok.type in _PY_SKIP:
                continue
            if tok.type == tokenize.INDENT:
                out.append("<indent>")
            elif tok.type == tokenize.DEDENT:
                out.append("<dedent>")
            elif tok.type == tokenize.NEWLINE:
                out.append("<nl>")
            elif tok.type == tokenize.COMMENT:
                out.append(tok.string.rstrip())
            else:
                out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return " ".join(out)


def normalise_code(text: str, language: str = "") -> str:
    """The unit's code with format-only differences removed, for the cache key."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if language in ("python", "py"):
        tokens = _normalise_python(text)
        if tokens is not None:
            return tokens
    return _normalise_lines(text)


def _key(material: list[str]) -> str:
    return hashlib.sha256(json.dumps(material).encode("utf-8")).hexdigest()


def review_key(*, provider: str, model: str, effort: str, instructions: str, unit: CodeUnit) -> str:
    return _key(["v2", provider, model, effort, instructions, unit.repo, unit.file_path, unit.symbol,
                 normalise_code(unit.raw_text, unit.language)])


def legacy_review_key(*, provider: str, model: str, effort: str, instructions: str,
                      unit: CodeUnit) -> str:
    """The key before normalisation: exact code. Read-only fallback, see the module docstring."""
    return _key([provider, model, effort, instructions, unit.repo, unit.file_path, unit.symbol,
                 unit.raw_text])


class ReviewCache:
    def __init__(self, path: str | Path, ttl_days: float = 60.0, ttl_jitter: float = 0.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_days * 86400 if ttl_days > 0 else None
        self.ttl_jitter = min(max(ttl_jitter, 0.0), 0.9)
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
            CREATE TABLE IF NOT EXISTS batch_results (
                batch_id TEXT NOT NULL,
                custom_id TEXT NOT NULL,
                PRIMARY KEY (batch_id, custom_id)
            );
            """
        )
        self._conn.commit()

    @classmethod
    def from_env(cls) -> ReviewCache | None:
        if os.getenv("AI_AUDIT_LLM_CACHE", "1").strip().lower() in {"0", "false", "no", "off"}:
            return None
        return cls(
            os.getenv("AI_AUDIT_LLM_CACHE_PATH", DEFAULT_PATH),
            ttl_days=float(os.getenv("AI_AUDIT_LLM_CACHE_TTL_DAYS", "60")),
            ttl_jitter=float(os.getenv("AI_AUDIT_LLM_CACHE_TTL_JITTER", "0.25")),
        )

    def ttl_for(self, key: str) -> float | None:
        """This key's TTL in seconds: the base TTL spread by ±jitter, the same on every call."""
        if self.ttl_s is None:
            return None
        u = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
        return self.ttl_s * (1 + self.ttl_jitter * (2 * u - 1))

    def _fresh(self, key: str) -> tuple[str, str, float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT response, model, created_at FROM reviews WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        ttl = self.ttl_for(key)
        if ttl is not None and time.time() - row[2] > ttl:
            return None
        return str(row[0]), str(row[1]), float(row[2])

    def get(self, key: str, *legacy: str) -> str | None:
        """The cached review under ``key``, else under a ``legacy`` key, which is then copied
        to ``key`` keeping its age (so it still expires when it would have)."""
        hit = self._fresh(key)
        if hit is not None:
            return hit[0]
        for old in legacy:
            hit = self._fresh(old)
            if hit is not None:
                # Each key has its own jittered TTL, so shift created_at by the difference:
                # the copy then expires exactly when the legacy entry would have.
                created = hit[2]
                new_ttl, old_ttl = self.ttl_for(key), self.ttl_for(old)
                if new_ttl is not None and old_ttl is not None:
                    created += old_ttl - new_ttl
                with self._lock:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO reviews (key, model, response, created_at) "
                        "VALUES (?, ?, ?, ?)", (key, hit[1], hit[0], created),
                    )
                    self._conn.commit()
                return hit[0]
        return None

    def has(self, key: str, *legacy: str) -> bool:
        return self.get(key, *legacy) is not None

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

    def harvested_results(self, batch_id: str) -> set[str]:
        """custom_ids of this batch whose result was already processed (usage recorded), so a
        harvest that died partway through doesn't bill them twice when it is retried."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT custom_id FROM batch_results WHERE batch_id = ?", (batch_id,)
            ).fetchall()
        return {r[0] for r in rows}

    def mark_result_harvested(self, batch_id: str, custom_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO batch_results (batch_id, custom_id) VALUES (?, ?)",
                (batch_id, custom_id),
            )
            self._conn.commit()

    def mark_harvested(self, batch_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE batches SET harvested = 1 WHERE batch_id = ?", (batch_id,))
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            p = self._conn.execute("SELECT COUNT(*) FROM batches WHERE harvested = 0").fetchone()[0]
        return {"reviews": int(n), "pending_batches": int(p)}
