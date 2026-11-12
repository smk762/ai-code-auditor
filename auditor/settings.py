from __future__ import annotations

import os
from dataclasses import dataclass

from auditor.env_loader import load_env_file

load_env_file()


def _int(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        return int(value)
    except ValueError:
        return default


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
    jwt_secret: str = os.getenv("JWT_SECRET", "change-me")
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
