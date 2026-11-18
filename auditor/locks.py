from __future__ import annotations

from contextlib import contextmanager
import logging
import threading
import uuid

import redis
from redis.exceptions import RedisError

from auditor.settings import get_settings

logger = logging.getLogger(__name__)

# Compare-and-act runs inside Redis so the token check and the write are one atomic step: a
# lock that expired and was re-acquired by another run between a GET and a DEL/PEXPIRE is never
# touched. It also keeps the token comparison out of Python.
_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)
_EXTEND_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
)


def _renew_until_stopped(
    client, key: str, token: str, ttl_s: int, stop: threading.Event, lock_name: str,
) -> None:
    """Keep extending the lock's TTL while the body runs, so a long run never silently loses it."""
    interval = max(ttl_s / 3, 0.05)
    while not stop.wait(interval):
        try:
            extended = client.eval(_EXTEND_SCRIPT, 1, key, token, int(ttl_s * 1000))
        except RedisError as exc:
            logger.warning("Pipeline lock %r: could not extend TTL (%s); retrying.", lock_name, exc)
            continue
        if not extended:
            logger.error(
                "Pipeline lock %r was lost (expired or taken by another run); "
                "this run no longer holds exclusivity.",
                lock_name,
            )
            return


@contextmanager
def pipeline_lock(lock_name: str, ttl_s: int = 3600):
    settings = get_settings()
    try:
        # Avoid multi-minute TCP hangs when Redis host is down or unroutable.
        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=10,
            socket_timeout=30,
        )
        token = str(uuid.uuid4())
        key = f"ai_audit_lock:{lock_name}"
        acquired = client.set(key, token, nx=True, ex=ttl_s)
        if not acquired:
            raise RuntimeError(f"Another pipeline run holds lock: {lock_name}")
        logger.info("Pipeline lock %r acquired in Redis.", lock_name)
    except RedisError as exc:
        if settings.redis_lock_required:
            raise
        logger.warning(
            "Pipeline lock: Redis error (%s) — continuing without distributed lock. "
            "To require Redis, set REDIS_LOCK_REQUIRED=1.",
            exc,
        )
        # Graceful fallback in local/test environments without Redis.
        yield
        return
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_renew_until_stopped,
        args=(client, key, token, ttl_s, stop, lock_name),
        name=f"pipeline-lock-{lock_name}",
        daemon=True,
    )
    heartbeat.start()
    try:
        yield
    finally:
        stop.set()
        heartbeat.join(timeout=5)
        try:
            client.eval(_RELEASE_SCRIPT, 1, key, token)
        except RedisError as exc:
            # Never let a release blip replace the body's own outcome; the TTL clears the key.
            logger.warning("Pipeline lock %r: release failed (%s); TTL will clear it.", lock_name, exc)
