"""batch_review._harvest and ReviewCache: retried harvests, answering model, legacy-key expiry."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import auditor.batch_review as br
from auditor.llm_cache import ReviewCache


class _Ledger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def record_usage(self, lane: str, model: str, usage: Any, **kw: Any) -> float:
        self.calls.append((lane, model))
        return 1.0


def _result(cid: str, model: str = "claude-haiku-5-5", text: str = "[]") -> SimpleNamespace:
    msg = SimpleNamespace(model=model, content=[SimpleNamespace(type="text", text=text)],
                          usage=SimpleNamespace(input_tokens=10, output_tokens=5))
    return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="succeeded", message=msg))


class _Batches:
    def __init__(self, results: list[SimpleNamespace], die_after: int | None = None) -> None:
        self._results = results
        self.die_after = die_after

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(processing_status="ended", request_counts=SimpleNamespace(processing=0))

    def results(self, batch_id: str):
        for i, r in enumerate(self._results):
            if self.die_after is not None and i >= self.die_after:
                raise ConnectionError("stream dropped")
            yield r


def _client(batches: _Batches) -> SimpleNamespace:
    return SimpleNamespace(messages=SimpleNamespace(batches=batches))


def test_a_harvest_retried_after_a_failure_bills_each_result_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _Ledger()
    monkeypatch.setattr(br, "get_ledger", lambda: ledger)
    cache = ReviewCache(tmp_path / "c.db")
    cache.add_batch("b1", "claude-haiku-5-5")
    batches = _Batches([_result("k1"), _result("k2"), _result("k3")], die_after=2)

    with pytest.raises(ConnectionError):
        br._harvest(_client(batches), cache, "b1", {}, "claude-haiku-5-5", br.BatchStats(), 0, 10)
    assert len(ledger.calls) == 2
    assert cache.pending_batches() == ["b1"]

    batches.die_after = None
    stats = br.BatchStats()
    assert br._harvest(_client(batches), cache, "b1", {}, "claude-haiku-5-5", stats, 0, 10)
    assert len(ledger.calls) == 3  # k3 only; k1 and k2 were already billed
    assert stats.cost_usd == 1.0
    assert cache.pending_batches() == []
    assert all(cache.get(k) == "[]" for k in ("k1", "k2", "k3"))


def test_harvested_reviews_record_the_model_that_answered(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(br, "get_ledger", lambda: _Ledger())
    cache = ReviewCache(tmp_path / "c.db")
    batches = _Batches([_result("k1", model="claude-sonnet-5-5")])
    br._harvest(_client(batches), cache, "b1", {}, "claude-haiku-5-5", br.BatchStats(), 0, 10)
    assert cache._fresh("k1")[1] == "claude-sonnet-5-5"


def test_legacy_copy_expires_when_the_legacy_entry_would_have(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "c.db", ttl_days=60, ttl_jitter=0.25)
    # Pick two keys with clearly different jittered TTLs.
    old, new = "legacy-a", next(k for k in (f"new-{i}" for i in range(100))
                                if abs(c.ttl_for(k) - c.ttl_for("legacy-a")) > 86400)
    c.put(old, "m", "[]")
    created = time.time() - 30 * 86400
    c._conn.execute("UPDATE reviews SET created_at = ? WHERE key = ?", (created, old))
    c._conn.commit()

    assert c.get(new, old) == "[]"
    copied_created = c._conn.execute("SELECT created_at FROM reviews WHERE key = ?", (new,)).fetchone()[0]
    assert copied_created + c.ttl_for(new) == pytest.approx(created + c.ttl_for(old))
