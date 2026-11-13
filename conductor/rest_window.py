"""Rest-window scheduling helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


@dataclass(slots=True)
class RestWindowConfig:
    timezone: str
    start: time
    end: time
    wrap_up_before_end_minutes: int
    inflight_grace_s: int
    max_tasks_per_session: int
    name: str = "nightly"


def load_rest_windows(path: str | Path = "config/rest_windows.yaml") -> RestWindowConfig:
    raw: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    windows = raw.get("windows") or [{"name": "nightly", "start": "00:30", "end": "06:00"}]
    w0 = windows[0]
    tz = str(raw.get("timezone") or "UTC")

    def _parse(hhmm: str) -> time:
        h, m = hhmm.split(":")
        return time(int(h), int(m))

    return RestWindowConfig(
        timezone=tz,
        start=_parse(str(w0.get("start", "00:30"))),
        end=_parse(str(w0.get("end", "06:00"))),
        wrap_up_before_end_minutes=int(raw.get("wrap_up_before_end_minutes", 20)),
        inflight_grace_s=int(raw.get("inflight_grace_s", 120)),
        max_tasks_per_session=int(raw.get("max_tasks_per_session", 12)),
        name=str(w0.get("name", "nightly")),
    )


def _now(cfg: RestWindowConfig) -> datetime:
    return datetime.now(ZoneInfo(cfg.timezone))


def in_rest_window(cfg: RestWindowConfig, now: datetime | None = None) -> bool:
    now = now or _now(cfg)
    t = now.timetz().replace(tzinfo=None)
    if cfg.start <= cfg.end:
        return cfg.start <= t < cfg.end
    # wraps midnight
    return t >= cfg.start or t < cfg.end


def wrap_up_deadline(cfg: RestWindowConfig, now: datetime | None = None) -> datetime:
    now = now or _now(cfg)
    end_dt = datetime.combine(now.date(), cfg.end, tzinfo=ZoneInfo(cfg.timezone))
    if cfg.start > cfg.end and now.timetz().replace(tzinfo=None) >= cfg.start:
        end_dt = end_dt + timedelta(days=1)
    elif cfg.start <= cfg.end and now.timetz().replace(tzinfo=None) >= cfg.end:
        end_dt = end_dt + timedelta(days=1)
    return end_dt - timedelta(minutes=cfg.wrap_up_before_end_minutes)


def should_freeze_launches(cfg: RestWindowConfig, now: datetime | None = None) -> bool:
    now = now or _now(cfg)
    if not in_rest_window(cfg, now):
        return True
    return now >= wrap_up_deadline(cfg, now)


def next_window_start_label(cfg: RestWindowConfig, now: datetime | None = None) -> str:
    now = now or _now(cfg)
    start_dt = datetime.combine(now.date(), cfg.start, tzinfo=ZoneInfo(cfg.timezone))
    if now.timetz().replace(tzinfo=None) >= cfg.start:
        start_dt = start_dt + timedelta(days=1)
    return start_dt.strftime("%Y-%m-%d %H:%M %Z")
