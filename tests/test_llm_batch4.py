"""conductor.llm: a spent Anthropic lane skips only that fallback, not the rest of the chain."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import conductor.llm as cl
from auditor.spend_ledger import BudgetExceeded, get_ledger


@pytest.fixture
def spent_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_SPEND_DB", str(tmp_path / "spend.db"))
    monkeypatch.delenv("AI_AUDIT_BUDGET_TOTAL_USD", raising=False)
    monkeypatch.delenv("AI_AUDIT_BUDGET_RESET_DAY", raising=False)
    monkeypatch.setenv("AI_AUDIT_BUDGET_CONDUCTOR_USD", "0.01")
    get_ledger().record("conductor", "claude-opus-5-5", 0, 1_000_000)  # $20, lane spent


def _litellm(monkeypatch: pytest.MonkeyPatch, fallbacks: list[str], fail: set[str]) -> list[str]:
    calls: list[str] = []

    def completion(**kw: Any) -> Any:
        calls.append(kw["model"])
        if kw["model"] in fail:
            raise RuntimeError(f"{kw['model']} down")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=kw["model"]))],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    mod = ModuleType("litellm")
    mod.completion = completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", mod)
    monkeypatch.setattr(cl, "load_conductor_models", lambda: cl.ConductorModels(
        conductor="openai/gpt-x", workers="x", pr_review="x", fallbacks=fallbacks,
    ))
    return calls


def test_spent_anthropic_fallback_moves_on_to_the_next(spent_lane: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _litellm(monkeypatch, ["anthropic/claude-opus-5-5", "xai/grok-x"], fail={"openai/gpt-x"})
    out = cl.completion([{"role": "user", "content": "hi"}])
    assert out == "xai/grok-x"
    assert calls == ["openai/gpt-x", "xai/grok-x"]  # the Anthropic fallback was never called


def test_all_fallbacks_exhausted_keeps_the_primary_failure_as_cause(
    spent_lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _litellm(monkeypatch, ["anthropic/claude-opus-5-5", "xai/grok-x"],
                     fail={"openai/gpt-x", "xai/grok-x"})
    with pytest.raises(BudgetExceeded) as ei:
        cl.completion([{"role": "user", "content": "hi"}])
    assert "openai/gpt-x down" in str(ei.value.__cause__)
    assert calls == ["openai/gpt-x", "xai/grok-x"]
