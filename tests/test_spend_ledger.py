from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from auditor.spend_ledger import (
    BudgetExceeded,
    SpendLedger,
    budget_month_start,
    cost_usd,
    get_ledger,
)


@pytest.fixture
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "spend.db"
    monkeypatch.setenv("AI_AUDIT_SPEND_DB", str(path))
    for k in ("AI_AUDIT_BUDGET_TOTAL_USD", "AI_AUDIT_BUDGET_AUDIT_USD", "AI_AUDIT_BUDGET_RESET_DAY"):
        monkeypatch.delenv(k, raising=False)
    return path


def test_cost_math() -> None:
    assert cost_usd("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert cost_usd("anthropic/claude-opus-5-5", 0, 1_000_000, batch=True) == pytest.approx(10.0)
    assert cost_usd("claude-haiku-5-5", 0, 0, cache_read=1_000_000) == pytest.approx(0.01)


def test_budget_month_start_respects_reset_day() -> None:
    assert budget_month_start(date(2026, 11, 13), reset_day=27) == date(2026, 10, 27)
    assert budget_month_start(date(2026, 11, 27), reset_day=27) == date(2026, 11, 27)
    assert budget_month_start(date(2026, 1, 5), reset_day=27) == date(2025, 12, 27)
    assert budget_month_start(date(2026, 11, 13), reset_day=1) == date(2026, 11, 1)


def test_lane_and_total_caps(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_BUDGET_AUDIT_USD", "1.00")
    monkeypatch.setenv("AI_AUDIT_BUDGET_TOTAL_USD", "5.00")
    led = SpendLedger(ledger_env)
    led.record("audit", "claude-haiku-5-5", 0, 2_000_000)  # $1.00
    with pytest.raises(BudgetExceeded):
        led.check("audit")
    led.check("conductor")  # other lane still fine
    led.record_manual("eval", 4.5)
    with pytest.raises(BudgetExceeded):
        led.check("conductor")  # total cap now hit
    s = led.summary()
    assert s["total_spent"] == pytest.approx(5.5)
    assert s["lanes"]["audit"]["remaining"] < 0


def test_conductor_records_usage_and_enforces_budget(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    import conductor.llm as cl

    monkeypatch.setitem(sys.modules, "litellm", None)  # requests path
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("AI_AUDIT_BUDGET_CONDUCTOR_USD", "0.05")

    class Resp:
        ok = True

        def json(self) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10_000, "output_tokens": 1_000}}

    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **kw: Resp())
    assert cl.completion([{"role": "user", "content": "hi"}], model="anthropic/claude-opus-5-5") == "ok"
    led = get_ledger()
    assert led.spent("conductor") == pytest.approx((10_000 * 4 + 1_000 * 20) / 1e6)  # $0.06
    with pytest.raises(BudgetExceeded):
        cl.completion([{"role": "user", "content": "hi"}], model="anthropic/claude-opus-5-5")


def test_batch_prefetch_trims_to_budget(ledger_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import auditor.batch_review as br
    from auditor.config import RepoConfig
    from auditor.contracts import CodeUnit

    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "1")
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("AI_AUDIT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("AI_AUDIT_LLM_MODEL", "claude-opus-5-5")
    units = [
        CodeUnit(id=f"u{i}", repo="r", file_path="r/a.py", symbol=f"s{i}", kind="function",
                 language="python", start_line=1, end_line=2,
                 raw_text=("def check_auth(token):\n" if i == 3 else "def f():\n") + "    x = 1\n" * 50)
        for i in range(5)
    ]
    monkeypatch.setattr(br, "resolve_scan_root", lambda repo: Path("/tmp"))
    monkeypatch.setattr(br, "scan_repo", lambda repo: ["/tmp/a.py"])
    monkeypatch.setattr(br, "extract_code_units", lambda name, f: units)
    monkeypatch.setattr(br, "build_repo_briefing", lambda name, root: "")
    one = br.estimate_usd("claude-opus-5-5", "x" * 4000)
    monkeypatch.setenv("AI_AUDIT_BUDGET_AUDIT_BATCH_USD", f"{one * 2.5:.6f}")

    stats = br.prefetch_reviews([RepoConfig(name="r", path="/tmp")], dry_run=True)
    assert stats.candidates == 5
    assert 1 <= stats.candidates - stats.deferred_for_budget < 5
    assert stats.estimated_usd <= one * 2.5


def test_cache_only_never_calls_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from auditor.config import ModelProviderConfig
    from auditor.contracts import CodeUnit
    from auditor.llm_client import LLMClient

    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "1")
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE_ONLY", "1")
    c = LLMClient(provider_config=ModelProviderConfig(provider="anthropic", model="claude-opus-5-5", endpoint=""))
    monkeypatch.setattr(c, "generate", lambda p: pytest.fail("cache-only must not call the model"))
    u = CodeUnit(id="u", repo="r", file_path="r/a.py", symbol="f", kind="function", language="python",
                 start_line=1, end_line=1, raw_text="def f(): pass")
    assert c.analyze_code(u) == [] and c.cache_misses == 1
    _ = SimpleNamespace  # keep import used
