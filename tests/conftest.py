from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_llm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from reading or writing the real review cache under index/."""
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "0")


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
