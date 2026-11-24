"""conductor.llm: a spent lane stops every cloud call and never a local one; every call is recorded (#172)."""

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


OLLAMA = "ollama/qwen"


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_SPEND_DB", str(tmp_path / "spend.db"))
    for k in ("AI_AUDIT_BUDGET_TOTAL_USD", "AI_AUDIT_BUDGET_CONDUCTOR_USD", "AI_AUDIT_BUDGET_RESET_DAY"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("model", ["openai/gpt-x", "xai/grok-x", "anthropic/claude-opus-5-5"])
def test_a_spent_lane_refuses_every_cloud_model(spent_lane: None, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    calls = _litellm(monkeypatch, [], fail=set())
    with pytest.raises(BudgetExceeded):
        cl.completion([{"role": "user", "content": "hi"}], model=model)
    assert calls == []


def test_a_spent_lane_refuses_without_litellm_too(spent_lane: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(BudgetExceeded):
        cl.completion([{"role": "user", "content": "hi"}], model="xai/grok-x")


def test_a_spent_lane_skips_cloud_fallbacks_and_runs_a_local_one(
    spent_lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _litellm(monkeypatch, ["anthropic/claude-opus-5-5", "xai/grok-x", "ollama/local"], fail={OLLAMA})
    assert cl.completion([{"role": "user", "content": "hi"}], model=OLLAMA) == "ollama/local"
    assert calls == [OLLAMA, "ollama/local"]


def test_a_spent_lane_with_no_local_fallback_keeps_the_primary_failure_as_cause(
    spent_lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _litellm(monkeypatch, ["anthropic/claude-opus-5-5", "xai/grok-x"], fail={OLLAMA})
    with pytest.raises(BudgetExceeded) as ei:
        cl.completion([{"role": "user", "content": "hi"}], model=OLLAMA)
    assert f"{OLLAMA} down" in str(ei.value.__cause__)
    assert calls == [OLLAMA]


@pytest.mark.parametrize("fail", [set(), {"openai/gpt-x"}])
def test_every_litellm_call_is_recorded(ledger: None, monkeypatch: pytest.MonkeyPatch, fail: set[str]) -> None:
    _litellm(monkeypatch, ["xai/grok-x"], fail=fail)
    used = cl.completion([{"role": "user", "content": "hi"}], note="n1")
    rows = get_ledger().rows_since(0)
    assert [(r[1], r[2], r[3], r[4], r[5]) for r in rows] == [("conductor", used.split("/")[1], "n1", 1, 1)]
    assert rows[0][7] > 0


def test_a_local_call_is_recorded_as_free(ledger: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _litellm(monkeypatch, [], fail=set())
    cl.completion([{"role": "user", "content": "hi"}], model=OLLAMA)
    assert [r[7] for r in get_ledger().rows_since(0)] == [0.0]
