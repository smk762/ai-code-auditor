"""Ensure the configured PostgreSQL database exists before Alembic runs migrations.

Run as:  python -m auditor.bootstrap_db
or call: ensure_database_exists() from startup code.

Skips silently for SQLite (file is created on first access).
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import create_engine, text
from sqlalchemy import pool as sa_pool
from sqlalchemy.engine.url import make_url

from auditor.settings import get_settings, normalize_database_url

logger = logging.getLogger(__name__)


def ensure_database_exists() -> None:
    """Create the database named in DATABASE_URL if it does not already exist."""
    settings = get_settings()
    raw_url = settings.database_url

    if raw_url.startswith("sqlite"):
        logger.info("bootstrap_db: SQLite detected — no create-database step needed")
        return

    normalized = normalize_database_url(raw_url)
    url = make_url(normalized)
    db_name = url.database

    if not db_name:
        raise RuntimeError("DATABASE_URL must include a database name (e.g. .../ai_audit)")

    # Connect to the maintenance DB ("postgres") to check / create the target DB.
    # AUTOCOMMIT is required — CREATE DATABASE cannot run inside a transaction.
    admin_url = url.set(database="postgres")
    engine = create_engine(
        admin_url.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
        poolclass=sa_pool.NullPool,
    )
    try:
        with engine.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            )
            if exists:
                logger.info("bootstrap_db: database %r already exists", db_name)
            else:
                # Quote the name to handle hyphens / mixed case safely.
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                logger.info("bootstrap_db: created database %r", db_name)
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        ensure_database_exists()
    except Exception as exc:
        logger.error("bootstrap_db: %s", exc)
        sys.exit(1)
