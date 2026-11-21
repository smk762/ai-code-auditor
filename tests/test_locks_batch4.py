"""Lock loss reaches the lock holder (pack 618909291e)."""
from __future__ import annotations

import threading
import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from auditor import locks
from tests.test_locks_packs import FakeRedis


@pytest.fixture
def fake(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(locks.redis, "from_url", lambda *a, **k: client)
    return client


def test_a_lock_taken_by_another_run_is_seen_by_the_body(fake):
    with pytest.raises(locks.LockLostError):
        with locks.pipeline_lock("nightly", ttl_s=0.15) as lock:
            fake.store["ai_audit_lock:nightly"] = b"other-run-token"
            assert lock.lost.wait(2.0)
            with pytest.raises(locks.LockLostError):
                lock.ensure_held()
    # The other run's lock is left alone.
    assert fake.store["ai_audit_lock:nightly"] == b"other-run-token"


def test_a_held_lock_lets_the_body_run(fake):
    with locks.pipeline_lock("nightly", ttl_s=0.15) as lock:
        time.sleep(0.3)
        lock.ensure_held()
        assert not lock.lost.is_set()


def test_body_exception_wins_over_lock_loss(fake):
    with pytest.raises(ValueError):
        with locks.pipeline_lock("nightly", ttl_s=0.15) as lock:
            fake.store["ai_audit_lock:nightly"] = b"other"
            lock.lost.wait(2.0)
            raise ValueError("body")


def test_redis_errors_past_the_ttl_count_as_loss():
    class Broken:
        def eval(self, *a, **k):
            raise RedisConnectionError("down")

    stop, lost = threading.Event(), threading.Event()
    t = threading.Thread(target=locks._renew_until_stopped,
                         args=(Broken(), "k", "tok", 0.15, stop, "nightly", lost), daemon=True)
    t.start()
    assert lost.wait(2.0)
    stop.set()
