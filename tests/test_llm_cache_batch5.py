"""A legacy-key hit replaces an expired row under the new key, and never a newer one."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from auditor.llm_cache import ReviewCache


def _row(path: Path, key: str) -> tuple[str, float]:
    with sqlite3.connect(str(path)) as conn:
        return conn.execute("SELECT response, created_at FROM reviews WHERE key = ?", (key,)).fetchone()


def _age(cache: ReviewCache, key: str, days: float) -> None:
    with cache._lock:
        cache._conn.execute("UPDATE reviews SET created_at = ? WHERE key = ?", (time.time() - days * 86400, key))
        cache._conn.commit()


def test_a_legacy_hit_replaces_an_expired_row_under_the_new_key(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    cache = ReviewCache(db, ttl_days=10)
    cache.put("new", "m", "stale")
    _age(cache, "new", 30)
    cache.put("old", "m", "fresh-legacy")
    _age(cache, "old", 1)

    assert cache.get("new", "old") == "fresh-legacy"
    assert _row(db, "new")[0] == "fresh-legacy"
    # The copy now answers on its own, without the legacy key.
    assert cache.get("new") == "fresh-legacy"


def test_a_legacy_hit_never_overwrites_a_newer_row(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    cache = ReviewCache(db, ttl_days=10)
    cache.put("old", "m", "legacy")
    _age(cache, "old", 5)
    cache.put("new", "m", "current")
    # A put racing the copy: the copy must not replace the newer row.
    hit = cache._fresh("old")
    assert hit is not None
    real_fresh = cache._fresh
    cache._fresh = lambda k: None if k == "new" else real_fresh(k)  # type: ignore[method-assign]
    assert cache.get("new", "old") == "legacy"
    assert _row(db, "new")[0] == "current"
