"""conductor.rest_window.wrap_up_deadline never returns a deadline that has already passed."""
from __future__ import annotations

from datetime import datetime, time

import pytest

from conductor.rest_window import RestWindowConfig, wrap_up_deadline


def _cfg(start: time, end: time) -> RestWindowConfig:
    return RestWindowConfig(timezone="UTC", start=start, end=end, wrap_up_before_end_minutes=20,
                            inflight_grace_s=120, max_tasks_per_session=12)


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 11, 14, 23, 0), datetime(2026, 11, 15, 5, 40)),   # in window, before midnight
    (datetime(2026, 11, 15, 1, 0), datetime(2026, 11, 15, 5, 40)),    # in window, after midnight
    (datetime(2026, 11, 15, 6, 0), datetime(2026, 11, 16, 5, 40)),    # the window just ended
    (datetime(2026, 11, 15, 14, 0), datetime(2026, 11, 16, 5, 40)),   # daytime, between end and start
])
def test_overnight_window_deadline(now: datetime, expected: datetime) -> None:
    cfg = _cfg(time(22, 30), time(6, 0))
    deadline = wrap_up_deadline(cfg, now)
    assert deadline.replace(tzinfo=None) == expected


def test_same_day_window_rolls_after_end() -> None:
    cfg = _cfg(time(0, 30), time(6, 0))
    assert wrap_up_deadline(cfg, datetime(2026, 11, 15, 1, 0)).replace(tzinfo=None) == datetime(2026, 11, 15, 5, 40)
    assert wrap_up_deadline(cfg, datetime(2026, 11, 15, 9, 0)).replace(tzinfo=None) == datetime(2026, 11, 16, 5, 40)
