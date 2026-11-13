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


def test_usage_endpoints() -> None:
    from fastapi.testclient import TestClient

    from auditor.audit_api import app

    client = TestClient(app)
    page = client.get("/audit/usage")
    assert page.status_code == 200 and "Claude API usage" in page.text
    assert client.get("/audit/usage.json?range=14d").json()["range"] == "14d"
    assert client.get("/audit/usage.json?range=bogus").status_code == 400
