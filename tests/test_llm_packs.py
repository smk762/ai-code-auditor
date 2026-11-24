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
    _fake_litellm(monkeypatch, "ollama/qwen", calls)
    with pytest.raises(BudgetExceeded):
        cl.completion([{"role": "user", "content": "hi"}])
    assert [c["model"] for c in calls] == ["ollama/qwen"]  # the fallback never ran


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


def test_a_billed_primary_call_never_reaches_the_fallbacks(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def completion(**kw: Any) -> Any:
        calls.append(kw)
        if kw["model"] == "anthropic/claude-opus-5-5":
            return SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="fb"))], usage=None)

    mod = ModuleType("litellm")
    mod.completion = completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", mod)
    monkeypatch.setattr(cl, "load_conductor_models", lambda: cl.ConductorModels(
        conductor="anthropic/claude-opus-5-5", workers="x", pr_review="x", fallbacks=["openai/gpt-x"],
    ))
    with pytest.raises(IndexError):
        cl.completion([{"role": "user", "content": "hi"}])
    assert [c["model"] for c in calls] == ["anthropic/claude-opus-5-5"]
    assert get_ledger().rows_since(0)  # the billed call is recorded


def _openai_body(monkeypatch: pytest.MonkeyPatch, model: str, status: int = 200, **kw: Any) -> dict[str, Any]:
    monkeypatch.setitem(sys.modules, "litellm", None)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("XAI_API_KEY", "test")
    import requests

    seen: dict[str, Any] = {}

    class Resp:
        status_code = status
        text = '{"error": {"message": "Unsupported parameter: max_tokens"}}'

        def raise_for_status(self) -> None:
            if status >= 400:
                raise requests.HTTPError(f"{status} Client Error")

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    def post(url: str, json: Any = None, **_: Any) -> Resp:
        seen.update(json)
        return Resp()

    monkeypatch.setattr(requests, "post", post)
    cl.completion([{"role": "user", "content": "hi"}], model=model, **kw)
    return seen


def test_openai_reasoning_model_gets_max_completion_tokens_and_effort(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _openai_body(monkeypatch, "openai/gpt-5", temperature=0.1, max_tokens=99, effort="xhigh")
    assert body["max_completion_tokens"] == 99 and "max_tokens" not in body
    assert "temperature" not in body
    assert body["reasoning_effort"] == "high"


def test_openai_chat_model_keeps_temperature(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _openai_body(monkeypatch, "openai/gpt-4o", temperature=0.1, max_tokens=99)
    assert body["max_completion_tokens"] == 99 and body["temperature"] == 0.1


def test_xai_keeps_max_tokens(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _openai_body(monkeypatch, "xai/grok-4", max_tokens=99)
    assert body["max_tokens"] == 99


def test_litellm_path_maps_effort_for_openai_reasoning(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def completion(**kw: Any) -> Any:
        calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None)

    mod = ModuleType("litellm")
    mod.completion = completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", mod)
    cl.completion([{"role": "user", "content": "hi"}], model="openai/o3", effort="medium", temperature=0.2)
    assert calls[0]["reasoning_effort"] == "medium" and "temperature" not in calls[0]


def test_openai_error_carries_the_api_message(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="Unsupported parameter"):
        _openai_body(monkeypatch, "openai/gpt-5", status=400)
