from __future__ import annotations

from contextlib import contextmanager
import uuid

import redis
from redis.exceptions import RedisError

from auditor.settings import get_settings


@contextmanager
def pipeline_lock(lock_name: str, ttl_s: int = 3600):
    settings = get_settings()
    try:
        client = redis.from_url(settings.redis_url)
        token = str(uuid.uuid4())
        key = f"ai_audit_lock:{lock_name}"
        acquired = client.set(key, token, nx=True, ex=ttl_s)
        if not acquired:
            raise RuntimeError(f"Another pipeline run holds lock: {lock_name}")
    except RedisError:
        if settings.redis_lock_required:
            raise
        # Graceful fallback in local/test environments without Redis.
        yield
        return
    try:
        yield
    finally:
        current = client.get(key)
        if current and current.decode("utf-8") == token:
            client.delete(key)
