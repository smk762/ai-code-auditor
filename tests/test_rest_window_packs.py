"""conductor.rest_window: a caller's ``now`` is read in the window's timezone."""
from __future__ import annotations

from datetime import UTC, datetime, time

from conductor.rest_window import (
    RestWindowConfig,
    in_rest_window,
    next_window_start_label,
    should_freeze_launches,
    wrap_up_deadline,
)


def _perth() -> RestWindowConfig:
    return RestWindowConfig(timezone="Australia/Perth", start=time(0, 30), end=time(6, 0),
                            wrap_up_before_end_minutes=20, inflight_grace_s=120, max_tasks_per_session=12)


def test_aware_now_in_another_zone_is_converted() -> None:
    cfg = _perth()
    # 17:00 UTC is 01:00 in Perth (UTC+8): inside the window, though 17:00 is not.
    utc = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)
    assert in_rest_window(cfg, utc)
    assert not should_freeze_launches(cfg, utc)
    assert wrap_up_deadline(cfg, utc) == datetime(2026, 11, 14, 5, 40, tzinfo=wrap_up_deadline(cfg).tzinfo)
    # 01:00 UTC is 09:00 in Perth: outside, though 01:00 is inside.
    assert not in_rest_window(cfg, datetime(2026, 11, 14, 1, 0, tzinfo=UTC))
    assert next_window_start_label(cfg, datetime(2026, 11, 14, 1, 0, tzinfo=UTC)).startswith("2026-11-15 00:30")


def test_naive_now_is_wall_time_in_the_window_zone() -> None:
    cfg = _perth()
    assert in_rest_window(cfg, datetime(2026, 11, 14, 1, 0))
    assert not should_freeze_launches(cfg, datetime(2026, 11, 14, 1, 0))
