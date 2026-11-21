"""alembic/env.py must accept a DATABASE_URL with percent-encoded characters."""

from __future__ import annotations

import io
from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"


def test_percent_encoded_database_url_survives_the_ini_config(monkeypatch, capsys):
    url = "postgresql://user:p%40ss%2Fword@localhost/ai_audit"
    monkeypatch.setenv("DATABASE_URL", url)
    # No ini file, so env.py skips fileConfig and leaves pytest's logging alone.
    cfg = Config(stdout=io.StringIO())
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))

    command.upgrade(cfg, "head", sql=True)  # offline: renders SQL, never connects

    assert cfg.get_main_option("sqlalchemy.url") == "postgresql+psycopg://user:p%40ss%2Fword@localhost/ai_audit"
    assert "CREATE TABLE repo_runs" in capsys.readouterr().out
