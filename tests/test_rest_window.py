from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from conductor.rest_window import RestWindowConfig, in_rest_window, should_freeze_launches
from datetime import time


def test_in_rest_window():
    cfg = RestWindowConfig(
        timezone="UTC",
        start=time(0, 30),
        end=time(6, 0),
        wrap_up_before_end_minutes=20,
        inflight_grace_s=120,
        max_tasks_per_session=12,
    )
    tz = ZoneInfo("UTC")
    assert in_rest_window(cfg, datetime(2026, 11, 11, 1, 0, tzinfo=tz))
    assert not in_rest_window(cfg, datetime(2026, 11, 11, 12, 0, tzinfo=tz))
    # freeze near end
    assert should_freeze_launches(cfg, datetime(2026, 11, 11, 5, 50, tzinfo=tz))


def test_rest_tz_env_overrides_file(tmp_path, monkeypatch):
    import pytest

    from conductor.rest_window import load_rest_windows

    cfg_file = tmp_path / "rest.yaml"
    cfg_file.write_text("timezone: UTC\nwindows:\n  - {name: nightly, start: '00:30', end: '06:00'}\n")
    assert load_rest_windows(cfg_file).timezone == "UTC"

    monkeypatch.setenv("AI_AUDIT_REST_TZ", "Australia/Perth")
    cfg = load_rest_windows(cfg_file)
    assert cfg.timezone == "Australia/Perth"
    # the conductor's clock now runs in Perth time: 00:35 Perth is inside the window and the
    # wrap-up deadline is 05:40 Perth (21:40 UTC), not 05:40 UTC (13:40 Perth)
    from conductor.rest_window import _now, wrap_up_deadline

    assert _now(cfg).tzinfo == ZoneInfo("Australia/Perth")
    perth = ZoneInfo("Australia/Perth")
    assert in_rest_window(cfg, datetime(2026, 11, 14, 0, 35, tzinfo=perth))
    deadline = wrap_up_deadline(cfg, datetime(2026, 11, 14, 0, 35, tzinfo=perth))
    assert deadline.astimezone(ZoneInfo("UTC")) == datetime(2026, 11, 13, 21, 40, tzinfo=ZoneInfo("UTC"))

    monkeypatch.setenv("AI_AUDIT_REST_TZ", "   ")
    assert load_rest_windows(cfg_file).timezone == "UTC"

    monkeypatch.setenv("AI_AUDIT_REST_TZ", "Mars/Olympus")
    with pytest.raises(ValueError, match="AI_AUDIT_REST_TZ"):
        load_rest_windows(cfg_file)
