"""Regression tests for auditor/locks.py (atomic release, TTL renewal, release errors)."""
from __future__ import annotations

import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from auditor import locks


class FakeRedis:
    """Just enough Redis for pipeline_lock: SET NX, GET, DEL and the two compare-and-act scripts."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.extends = 0
        self.fail_release = False
        self.on_get = None
        self.deleted_values: list[bytes | None] = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value.encode() if isinstance(value, str) else value
        return True

    def get(self, key):
        value = self.store.get(key)
        if self.on_get is not None:
            self.on_get(key)
        return value

    def delete(self, key):
        if self.fail_release:
            raise RedisConnectionError("redis gone")
        self.deleted_values.append(self.store.get(key))
        return 1 if self.store.pop(key, None) is not None else 0

    def eval(self, script, numkeys, key, token, *args):
        if self.fail_release:
            raise RedisConnectionError("redis gone")
        held = self.store.get(key) == str(token).encode()
        if "pexpire" in script:
            if held:
                self.extends += 1
            return 1 if held else 0
        if "del" in script:
            if held:
                self.deleted_values.append(self.store.pop(key))
                return 1
            return 0
        raise AssertionError(f"unexpected script {script!r}")


@pytest.fixture
def fake(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(locks.redis, "from_url", lambda *a, **k: client)
    return client


def test_release_never_deletes_a_lock_another_run_took_over(fake):
    with locks.pipeline_lock("nightly"):
        # If the release reads the token and deletes in two steps, the TTL expires right after
        # the read and another run acquires the lock before the delete lands.
        def take_over(k):
            fake.store[k] = b"other-run-token"
            fake.on_get = None
        fake.on_get = take_over
    assert b"other-run-token" not in fake.deleted_values


def test_release_frees_our_own_lock(fake):
    with locks.pipeline_lock("nightly"):
        assert "ai_audit_lock:nightly" in fake.store
    assert "ai_audit_lock:nightly" not in fake.store


def test_redis_error_on_release_does_not_mask_the_body_exception(fake):
    with pytest.raises(ValueError, match="real failure"):
        with locks.pipeline_lock("nightly"):
            fake.fail_release = True
            raise ValueError("real failure")


def test_redis_error_on_release_does_not_fail_a_successful_run(fake):
    with locks.pipeline_lock("nightly"):
        fake.fail_release = True


def test_lock_ttl_is_renewed_while_the_body_runs(fake):
    with locks.pipeline_lock("nightly", ttl_s=0.15):
        time.sleep(0.4)
    assert fake.extends >= 1
