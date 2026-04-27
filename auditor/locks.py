from __future__ import annotations

from contextlib import contextmanager
import logging
import uuid

import redis
from redis.exceptions import RedisError

from auditor.settings import get_settings

logger = logging.getLogger(__name__)


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
    try:
        yield
    finally:
        current = client.get(key)
        if current and current.decode("utf-8") == token:
            client.delete(key)
