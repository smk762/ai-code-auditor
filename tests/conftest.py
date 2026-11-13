from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_llm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from reading or writing the real review cache under index/."""
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "0")
