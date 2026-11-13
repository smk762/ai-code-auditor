"""Aggregations for the API usage dashboard (GET /audit/usage, /audit/usage.json).

Everything is computed from the spend ledger in local time, so "hungry periods" line up with
the timers (00:30 conductor, 01:30 batch review, hourly merge poll).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from auditor.spend_ledger import budget_month_start, get_ledger

HTML_PATH = Path(__file__).parent / "static" / "usage_dashboard.html"
TOP_PROCESSES = 12


def _next_reset(start: date) -> date:
    month = start.month % 12 + 1
    year = start.year + (1 if start.month == 12 else 0)
    return date(year, month, start.day)


def usage_data(range_key: str = "month") -> dict[str, Any]:
    ledger = get_ledger()
    now = datetime.now()
    today = now.date()
    m_start = budget_month_start(today)
    m_reset = _next_reset(m_start)

    if range_key == "month":
        r_start = m_start
    else:
        days = max(1, min(int(range_key.rstrip("d") or 14), 90))
        r_start = today - timedelta(days=days - 1)
    since = min(r_start, m_start)
    rows = ledger.rows_since(datetime(since.year, since.month, since.day).timestamp())

    month_spent = sum(r[7] for r in rows if datetime.fromtimestamp(r[0]).date() >= m_start)
    in_range = [r for r in rows if datetime.fromtimestamp(r[0]).date() >= r_start]

    # Burn rate from the last 7 days (or since month start if shorter), excluding manual entries
    # so one-off evals don't distort the projection.
    burn_from = max(m_start, today - timedelta(days=6))
    burn_days = (today - burn_from).days + 1
    burn = sum(r[7] for r in rows if r[2] != "manual"
               and datetime.fromtimestamp(r[0]).date() >= burn_from) / burn_days
    days_left = max((m_reset - today).days, 0)
    caps = ledger.caps()

    days_axis = [(r_start + timedelta(days=i)).isoformat() for i in range((today - r_start).days + 1)]
    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    heat: dict[str, list[float]] = {d: [0.0] * 24 for d in days_axis}
    by_lane: dict[str, float] = defaultdict(float)
    by_model: dict[str, float] = defaultdict(float)
    by_process: dict[str, dict[str, Any]] = {}
    hours: dict[tuple[str, int], dict[str, Any]] = {}
    tokens_in = tokens_out = calls = 0

    for ts, lane, model, note, tin, tout, batch, usd in in_range:
        dt = datetime.fromtimestamp(ts)
        d = dt.date().isoformat()
        daily[d][lane] += usd
        if d in heat:
            heat[d][dt.hour] += usd
        by_lane[lane] += usd
        by_model[model] += usd
        proc_name = f"{lane} · {note}" if note else f"{lane} · (unlabelled)"
        p = by_process.setdefault(proc_name, {"name": proc_name, "lane": lane, "cost": 0.0, "calls": 0,
                                              "input_tokens": 0, "output_tokens": 0})
        p["cost"] += usd
        p["calls"] += 1
        p["input_tokens"] += tin
        p["output_tokens"] += tout
        h = hours.setdefault((d, dt.hour), {"date": d, "hour": dt.hour, "cost": 0.0, "calls": 0,
                                            "lanes": defaultdict(float)})
        h["cost"] += usd
        h["calls"] += 1
        h["lanes"][lane] += usd
        tokens_in += tin
        tokens_out += tout
        calls += 1

    lanes = sorted(set(by_lane) | {k for k in caps if k != "total"},
                   key=lambda name: -by_lane.get(name, 0.0))
    procs = sorted(by_process.values(), key=lambda p: -p["cost"])
    top_procs = procs[:TOP_PROCESSES]
    if len(procs) > TOP_PROCESSES:
        rest = procs[TOP_PROCESSES:]
        top_procs.append({"name": f"Other ({len(rest)})", "lane": "other",
                          "cost": sum(p["cost"] for p in rest), "calls": sum(p["calls"] for p in rest),
                          "input_tokens": sum(p["input_tokens"] for p in rest),
                          "output_tokens": sum(p["output_tokens"] for p in rest)})
    top_hours = sorted(hours.values(), key=lambda h: -h["cost"])[:10]
    for h in top_hours:
        h["top_lane"] = max(h["lanes"].items(), key=lambda kv: kv[1])[0]
        h["lanes"] = dict(h["lanes"])

    # Cumulative spend across the budget month, for the burn-vs-cap chart.
    cum, running = [], 0.0
    month_days = [(m_start + timedelta(days=i)) for i in range((m_reset - m_start).days)]
    per_day_month: dict[date, float] = defaultdict(float)
    for r in rows:
        dd = datetime.fromtimestamp(r[0]).date()
        if dd >= m_start:
            per_day_month[dd] += r[7]
    for dd in month_days:
        if dd > today:
            cum.append({"date": dd.isoformat(), "actual": None,
                        "projected": round(running + burn * (dd - today).days, 4)})
            continue
        running += per_day_month.get(dd, 0.0)
        cum.append({"date": dd.isoformat(), "actual": round(running, 4),
                    "projected": round(running, 4) if dd == today else None})

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "range": range_key,
        "range_start": r_start.isoformat(),
        "month": {
            "start": m_start.isoformat(),
            "resets": m_reset.isoformat(),
            "days_left": days_left,
            "spent": round(month_spent, 4),
            "cap": caps.get("total"),
            "burn_per_day": round(burn, 4),
            "projected": round(month_spent + burn * days_left, 4),
        },
        "range_totals": {"cost": round(sum(by_lane.values()), 4), "calls": calls,
                         "input_tokens": tokens_in, "output_tokens": tokens_out},
        "lanes": [
            {"lane": name, "cost": round(by_lane.get(name, 0.0), 4),
             "month_spent": round(ledger.spent(name), 4), "cap": caps.get(name)}
            for name in lanes
        ],
        "models": [{"model": k, "cost": round(v, 4)} for k, v in sorted(by_model.items(), key=lambda kv: -kv[1])],
        "days": days_axis,
        "daily": {d: {k: round(v, 4) for k, v in daily[d].items()} for d in days_axis},
        "heatmap": {d: [round(x, 4) for x in v] for d, v in heat.items()},
        "processes": [{**p, "cost": round(p["cost"], 4)} for p in top_procs],
        "top_hours": [{**h, "cost": round(h["cost"], 4)} for h in top_hours],
        "cumulative": cum,
    }
