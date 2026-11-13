from __future__ import annotations

from typing import Any

import pytest

import auditor.llm_client as llm_mod
from auditor.config import ModelProviderConfig
from auditor.contracts import CodeUnit
from auditor.llm_client import LLMClient, _parse_findings_json


def _unit() -> CodeUnit:
    return CodeUnit(
        id="u1", repo="r", file_path="r/a.py", symbol="f", kind="function",
        language="python", start_line=10, end_line=12, raw_text="def f():\n    return 1\n",
    )


def _client(provider: str, model: str = "m", endpoint: str = "http://x/api/generate") -> LLMClient:
    return LLMClient(provider_config=ModelProviderConfig(provider=provider, model=model, endpoint=endpoint))


@pytest.mark.parametrize(
    "raw",
    [
        '[{"title": "t", "severity": "high"}]',
        '```json\n[{"title": "t", "severity": "high"}]\n```',
        '<think>hmm [maybe]</think>\nHere you go: [{"title": "t", "severity": "high"}]',
    ],
)
def test_parse_tolerates_fences_and_think_blocks(raw: str) -> None:
    findings = _parse_findings_json(raw, _unit())
    assert findings is not None and len(findings) == 1
    assert findings[0].severity == "HIGH"


def test_parse_empty_array_is_clean_not_failure() -> None:
    assert _parse_findings_json("[]", _unit()) == []
    assert _parse_findings_json("no json here", _unit()) is None


def test_parse_non_numeric_line_falls_back_to_unit_start() -> None:
    findings = _parse_findings_json('[{"title": "t", "line": "presigned_get_object"}]', _unit())
    assert findings is not None and findings[0].line == 10


def test_clean_llm_answer_skips_heuristics(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client("ollama")
    monkeypatch.setattr(c, "generate", lambda prompt: "[]")
    monkeypatch.setattr(llm_mod, "_heuristic_findings", lambda unit: pytest.fail("heuristics used"))
    assert c.analyze_code(_unit()) == []


def test_openai_compatible_sends_chat_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client("openai-compatible", endpoint="http://x/v1/chat/completions")
    seen: dict[str, Any] = {}

    def fake(payload: dict[str, Any], headers: dict[str, str]) -> str:
        seen.update(payload)
        return "[]"

    monkeypatch.setattr(c, "_do_generate", fake)
    assert c.generate("hi") == "[]"
    assert seen["messages"] == [{"role": "user", "content": "hi"}]
    assert "prompt" not in seen


def test_anthropic_routes_through_conductor_without_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    import conductor.llm as cl

    seen: dict[str, Any] = {}

    def fake_completion(messages: list[dict[str, str]], **kw: Any) -> str:
        seen.update(kw)
        return "[]"

    monkeypatch.setattr(cl, "completion", fake_completion)
    c = _client("anthropic", model="claude-haiku-5-5")
    assert c.generate("hi") == "[]"
    assert seen["model"] == "anthropic/claude-haiku-5-5"
    assert seen["temperature"] is None


def test_generate_failure_is_logged_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    c = _client("ollama")
    monkeypatch.setattr(llm_mod, "with_retries", lambda fn, **kw: fn())

    def boom(payload: dict[str, Any], headers: dict[str, str]) -> str:
        raise RuntimeError("404 model not found")

    monkeypatch.setattr(c, "_do_generate", boom)
    assert c.generate("hi") == ""
    assert "404 model not found" in caplog.text


def test_ollama_health_check_flags_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, Any]:
            return {"models": [{"name": "qwen3:8b"}]}

    monkeypatch.setattr(llm_mod.requests, "get", lambda url, timeout: Resp())
    ok, msg = _client("ollama", model="deepseek-coder-33b").health_check()
    assert not ok and "deepseek-coder-33b" in msg
    assert _client("ollama", model="qwen3:8b").health_check() == (True, "ok")


def test_conductor_never_sends_temperature_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    import conductor.llm as cl

    seen: dict[str, Any] = {}

    def fake_requests(model: str, messages: Any, **kw: Any) -> str:
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(cl, "_requests_completion", fake_requests)
    monkeypatch.setitem(__import__("sys").modules, "litellm", None)  # force requests path
    cl.completion([{"role": "user", "content": "hi"}], model="anthropic/claude-opus-5-5")
    assert seen["temperature"] is None


def test_anthropic_health_check_needs_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, msg = _client("anthropic").health_check()
    assert not ok and "ANTHROPIC_API_KEY" in msg
