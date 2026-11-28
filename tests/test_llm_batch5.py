"""conductor.llm: every model id `_is_anthropic` accepts gets the requested effort."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@pytest.mark.parametrize("model", ["claude-opus-4-5", "anthropic/claude-opus-4-5"], ids=["bare", "prefixed"])
def test_effort_reaches_every_anthropic_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    import conductor.llm as cl

    monkeypatch.setenv("AI_AUDIT_SPEND_DB", str(tmp_path / "spend.db"))
    for k in ("AI_AUDIT_BUDGET_TOTAL_USD", "AI_AUDIT_BUDGET_CONDUCTOR_USD", "AI_AUDIT_BUDGET_RESET_DAY"):
        monkeypatch.delenv(k, raising=False)
    sent: dict[str, Any] = {}

    def completion(**kw: Any) -> Any:
        sent.update(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    mod = ModuleType("litellm")
    mod.completion = completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", mod)
    assert cl.completion([{"role": "user", "content": "hi"}], model=model, effort="high") == "ok"
    assert sent["output_config"] == {"effort": "high"} and "temperature" not in sent
