from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

from auditor.env_loader import load_env_file

load_env_file()

logger = logging.getLogger(__name__)

# HS256 wants a key of at least 256 bits; anything shorter, or a known placeholder, is treated as
# "no secret configured".
JWT_MIN_SECRET_LEN = 32
_JWT_PLACEHOLDERS = {"", "change-me", "changeme", "change_me", "secret", "jwt-secret", "your-secret"}
# Without a real JWT_SECRET, tokens are checked against this random per-process key, which nobody
# can sign with: every JWT is refused (fail closed) while startup paths that never touch JWT
# (the API on X-API-Token, the CLI, the nightly pipelines) keep working.
_EPHEMERAL_JWT_SECRET = secrets.token_urlsafe(48)
_warned_weak_jwt_secret = False


def _int(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        return int(value)
    except ValueError:
        return default


def _jwt_secret() -> str:
    global _warned_weak_jwt_secret
    value = os.getenv("JWT_SECRET", "").strip()
    if value.lower() not in _JWT_PLACEHOLDERS and len(value) >= JWT_MIN_SECRET_LEN:
        return value
    if not _warned_weak_jwt_secret:
        _warned_weak_jwt_secret = True
        logger.warning(
            "JWT_SECRET is unset, a placeholder or shorter than %d chars: Bearer JWTs will be refused. "
            "Set a strong JWT_SECRET to enable JWT auth.",
            JWT_MIN_SECRET_LEN,
        )
    return _EPHEMERAL_JWT_SECRET


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "ai-code-auditor")
    env: str = os.getenv("ENV", "dev")
    request_timeout_ms: int = _int("REQUEST_TIMEOUT_MS", 8000)

    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./audit.db")
    db_pool_size: int = _int("DB_POOL_SIZE", 10)
    db_max_overflow: int = _int("DB_MAX_OVERFLOW", 20)
    db_pool_timeout: int = _int("DB_POOL_TIMEOUT", 30)

    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6380/0")

    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "")
    s3_access_key_id: str = os.getenv("S3_ACCESS_KEY_ID", "")
    s3_secret_access_key: str = os.getenv("S3_SECRET_ACCESS_KEY", "")
    s3_bucket: str = os.getenv("S3_BUCKET", "")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")

    jwt_issuer: str = os.getenv("JWT_ISSUER", "ai-code-auditor")
    jwt_audience: str = os.getenv("JWT_AUDIENCE", "ai-code-auditor")
    jwt_secret: str = field(default_factory=_jwt_secret)
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    llm_healthcheck_required: bool = _bool("LLM_HEALTHCHECK_REQUIRED", False)
    embedder_healthcheck_required: bool = _bool("EMBEDDER_HEALTHCHECK_REQUIRED", False)
    redis_lock_required: bool = _bool("REDIS_LOCK_REQUIRED", False)


def get_settings() -> Settings:
    return Settings()


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url
