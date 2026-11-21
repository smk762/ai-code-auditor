from __future__ import annotations

from datetime import datetime

import pytest

from auditor.spend_ledger import get_ledger
from auditor.usage_dashboard import usage_data


def test_usage_data_aggregates_by_lane_process_and_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_BUDGET_TOTAL_USD", "100")
    led = get_ledger()
    led.record("conductor", "claude-opus-5-5", 100_000, 10_000, note="security:t1")  # $0.60
    led.record("audit", "claude-haiku-5-5", 1_000_000, 0, note="argus-cortex")  # $0.10
    led.record("audit", "claude-haiku-5-5", 1_000_000, 0, note="argus-cortex")  # $0.10

    d = usage_data("7d")
    assert d["range_totals"]["calls"] == 3
    assert d["month"]["spent"] == pytest.approx(0.8)
    assert d["month"]["cap"] == 100
    procs = {p["name"]: p for p in d["processes"]}
    assert procs["audit · argus-cortex"]["calls"] == 2
    assert d["processes"][0]["name"] == "conductor · security:t1"  # hungriest first
    today = datetime.now().date().isoformat()
    assert d["daily"][today]["audit"] == pytest.approx(0.2)
    assert sum(d["heatmap"][today]) == pytest.approx(0.8)
    assert {m["model"] for m in d["models"]} == {"claude-opus-5-5", "claude-haiku-5-5"}
    assert d["top_hours"][0]["top_lane"] == "conductor"



def test_burn_rate_excludes_manual_and_baseline_one_offs() -> None:
    led = get_ledger()
    led.record_manual("eval", 15.0, note="evals")
    led.record("audit_batch", "claude-opus-5-5", 5_000_000, 0, batch=True, note="baseline:argus-mask")  # $10
    led.record("audit", "claude-haiku-5-5", 7_000_000, 0, note="argus-mask")  # $0.70

    m = usage_data("month")["month"]
    assert m["spent"] == pytest.approx(25.7)
    assert m["one_off"] == pytest.approx(25.0)
    burn_days = min(7, datetime.now().day)  # window is clipped to the budget-month start
    assert m["burn_per_day"] == pytest.approx(0.7 / burn_days, abs=1e-4)


def test_usage_endpoints() -> None:
    from fastapi.testclient import TestClient

    from auditor.audit_api import app

    client = TestClient(app)
    page = client.get("/audit/usage")
    assert page.status_code == 200 and "Claude API usage" in page.text
    assert client.get("/audit/usage.json?range=14d").json()["range"] == "14d"
    assert client.get("/audit/usage.json?range=bogus").status_code == 400


def test_next_reset_clamps_to_month_end():
    from datetime import date

    from auditor.usage_dashboard import _next_reset

    assert _next_reset(date(2026, 1, 31)) == date(2026, 2, 28)
    assert _next_reset(date(2028, 1, 30)) == date(2028, 2, 29)
    assert _next_reset(date(2026, 3, 31)) == date(2026, 4, 30)
    assert _next_reset(date(2026, 12, 15)) == date(2027, 1, 15)


@pytest.mark.parametrize("key", ["week", "7days", "all", "1.5d", ""])
def test_unparseable_range_falls_back_instead_of_raising(key: str) -> None:
    d = usage_data(key)
    assert d["range"] == key and d["range_start"]


def test_headline_projection_matches_the_chart_end() -> None:
    led = get_ledger()
    led.record("audit", "claude-haiku-5-5", 7_000_000, 0, note="argus-mask")  # $0.70 recurring
    d = usage_data("month")
    assert d["month"]["burn_per_day"] > 0
    assert d["month"]["projected"] == pytest.approx(d["cumulative"][-1]["projected"], abs=1e-3)


def _status_input(spent, cap, projected, days_left=9, lanes=()):
    return {"month": {"spent": spent, "cap": cap, "projected": projected, "days_left": days_left},
            "lanes": [{"lane": n, "month_spent": s, "cap": c} for n, s, c in lanes]}


def test_usage_json_carries_a_status_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_BUDGET_TOTAL_USD", "100")
    get_ledger().record("audit", "claude-haiku-5-5", 7_000_000, 0, note="argus-mask")  # $0.70
    d = usage_data("7d")
    assert d["summary"].startswith("Claude API spend this month: $0.70 of $100 cap (1%), projected $")
    assert len(d["summary"]) <= 200
    assert d["spoken"].startswith("Claude API spend this month is under a dollar of a 100 dollar cap")
    assert d["severity"] == "info"
    assert "month" in d and "lanes" in d  # additive: existing fields untouched


@pytest.mark.parametrize("args, severity", [
    ((10, 100, 50), "info"),
    ((10, 100, 85), "notice"),
    ((10, 100, 50, 9, [("audit", 13, 15)]), "notice"),
    ((10, 100, 120), "warning"),
    ((10, 100, 50, 9, [("audit", 15, 15)]), "warning"),
    ((100, 100, 140), "critical"),
    ((500, None, 900), "info"),
])
def test_status_severity_follows_the_dashboard_thresholds(args, severity) -> None:
    from auditor.usage_dashboard import usage_status

    assert usage_status(_status_input(*args))["severity"] == severity


def test_status_text_is_short_and_speakable() -> None:
    from auditor.usage_dashboard import usage_status

    lanes = [(f"lane_number_{i}", 20, 15) for i in range(30)]
    s = usage_status(_status_input(101.5, 190, 260.25, 1, lanes))
    assert len(s["summary"]) <= 200 and s["summary"].endswith("...")
    assert s["summary"].startswith("Claude API spend this month: $101.50 of $190 cap (53%), projected $260.25; "
                                   "resets in 1 day; at lane cap: lane_number_0")
    assert not set("$%_;:/") & set(s["spoken"])
    assert "goes over the cap" in s["spoken"] and s["spoken"].endswith("The budget resets in 1 day.")

    one = usage_status(_status_input(5, 190, 20, 3, [("audit_batch", 40, 40)]))
    assert "The audit batch lane has hit its cap." in one["spoken"]
    assert one["summary"].endswith("; at lane cap: audit_batch")
    assert "reached" not in one["spoken"]

    full = usage_status(_status_input(190, 190, 190))
    assert "cap is reached" in full["spoken"]
    assert usage_status(_status_input(3, None, 9))["summary"] == (
        "Claude API spend this month: $3.00 (no cap), projected $9.00; resets in 9 days")
