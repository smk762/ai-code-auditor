from __future__ import annotations

import os
import tempfile

import pytest

# Never touch the real audit DB. auditor.db binds its engine at import time from DATABASE_URL,
# and auditor.env_loader only setdefault()s from .env, so this must be set before any test
# module imports auditor.*. Without it, the pipeline smoke test wrote two runs into the
# production Postgres on every suite run from a checkout with a real .env.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="ai-audit-test-db-"), "audit.db")


@pytest.fixture(autouse=True)
def _no_real_llm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from reading or writing the real review cache under index/."""
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "0")


@pytest.fixture(autouse=True)
def _isolated_spend_ledger(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the real spend ledger under index/."""
    monkeypatch.setenv("AI_AUDIT_SPEND_DB", str(tmp_path_factory.mktemp("spend") / "spend.db"))


@pytest.fixture(autouse=True)
def _isolated_rag_ledger(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the real record of RAG doc ids (reports/rag_ingested.json)."""
    monkeypatch.setenv("AI_AUDIT_RAG_LEDGER", str(tmp_path_factory.mktemp("rag") / "rag_ingested.json"))


@pytest.fixture(autouse=True)
def _isolate_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """auditor.env_loader pulls the developer's real .env into os.environ; tests must
    see built-in defaults, not whichever provider/model is configured locally."""
    import os

    for name in list(os.environ):
        if name.startswith(("AI_AUDIT_LLM_", "AI_AUDIT_EMBEDDER_")) or name in {
            "AI_AUDIT_CONDUCTOR_MODEL", "AI_AUDIT_WORKER_MODEL", "AI_AUDIT_PR_REVIEW_MODEL",
        }:
            if name != "AI_AUDIT_LLM_CACHE":
                monkeypatch.delenv(name, raising=False)
    # the host's rest-window timezone override must not leak into tests
    monkeypatch.delenv("AI_AUDIT_REST_TZ", raising=False)
    # nor the host's rag-chat API key (cli repair sends it as a Bearer token)
    monkeypatch.delenv("RAG_CHAT_API_KEY", raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """API test client with output_dir pointed at tmp_path."""
    from fastapi.testclient import TestClient

    from auditor.audit_api import app

    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(tmp_path))
    return TestClient(app)


@pytest.fixture
def memdb(monkeypatch: pytest.MonkeyPatch):
    """Point the web UI's DB reads (auditor.ui._db_session) at a throwaway in-memory SQLite.

    StaticPool shares one connection, so TestClient's worker thread sees the same database.
    """
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from auditor import ui
    from auditor.db import Base

    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)

    @contextmanager
    def session():
        s = factory()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(ui, "_db_session", session)
    return session
