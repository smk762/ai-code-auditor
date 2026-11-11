from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from conductor.rest_window import RestWindowConfig, in_rest_window, should_freeze_launches
from datetime import time


def test_in_rest_window():
    cfg = RestWindowConfig(
        timezone="Australia/Perth",
        start=time(0, 30),
        end=time(6, 0),
        wrap_up_before_end_minutes=20,
        inflight_grace_s=120,
        max_tasks_per_session=12,
    )
    tz = ZoneInfo("Australia/Perth")
    assert in_rest_window(cfg, datetime(2026, 11, 11, 1, 0, tzinfo=tz))
    assert not in_rest_window(cfg, datetime(2026, 11, 11, 12, 0, tzinfo=tz))
    # freeze near end
    assert should_freeze_launches(cfg, datetime(2026, 11, 11, 5, 50, tzinfo=tz))
