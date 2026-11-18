"""conductor.llm: fallbacks follow the primary path's Anthropic rules; OpenAI spend is recorded."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import conductor.llm as cl
from auditor.spend_ledger import BudgetExceeded, get_ledger


@pytest.fixture
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "spend.db"
    monkeypatch.setenv("AI_AUDIT_SPEND_DB", str(path))
    for k in ("AI_AUDIT_BUDGET_TOTAL_USD", "AI_AUDIT_BUDGET_CONDUCTOR_USD", "AI_AUDIT_BUDGET_RESET_DAY"):
        monkeypatch.delenv(k, raising=False)
    return path


def _fake_litellm(monkeypatch: pytest.MonkeyPatch, primary: str, calls: list[dict[str, Any]]) -> None:
    def completion(**kw: Any) -> Any:
        calls.append(kw)
        if kw["model"] == primary:
            raise RuntimeError("primary down")
        if "temperature" in kw and kw["model"].startswith("anthropic/"):
            raise RuntimeError("400 temperature is deprecated for this model")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="from fallback"))],
            usage=SimpleNamespace(prompt_tokens=100_000, completion_tokens=10_000),
        )

    mod = ModuleType("litellm")
    mod.completion = completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", mod)
    monkeypatch.setattr(cl, "load_conductor_models", lambda: cl.ConductorModels(
        conductor=primary, workers=primary, pr_review=primary,
        fallbacks=["anthropic/claude-opus-5-5"],
    ))


def test_anthropic_fallback_gets_no_temperature_and_is_recorded(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _fake_litellm(monkeypatch, "openai/gpt-x", calls)
    out = cl.completion([{"role": "user", "content": "hi"}], temperature=0.1)
    assert out == "from fallback"
    assert calls[0]["temperature"] == 0.1  # primary (OpenAI) keeps it
    assert "temperature" not in calls[-1]
    assert get_ledger().spent("conductor") == pytest.approx((100_000 * 4 + 10_000 * 20) / 1e6)


def test_anthropic_fallback_is_budget_checked(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_BUDGET_CONDUCTOR_USD", "0.01")
    get_ledger().record("conductor", "claude-opus-5-5", 0, 1_000_000)  # $20, lane spent
    calls: list[dict[str, Any]] = []
    _fake_litellm(monkeypatch, "openai/gpt-x", calls)
    with pytest.raises(BudgetExceeded):
        cl.completion([{"role": "user", "content": "hi"}])
    assert [c["model"] for c in calls] == ["openai/gpt-x"]  # the fallback never ran


def test_openai_requests_path_records_usage(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    class Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1234, "completion_tokens": 56}}

    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **kw: Resp())
    assert cl.completion([{"role": "user", "content": "hi"}], model="openai/gpt-x", note="n1") == "ok"
    rows = get_ledger().rows_since(0)
    assert [(r[1], r[2], r[3], r[4], r[5]) for r in rows] == [("conductor", "gpt-x", "n1", 1234, 56)]
