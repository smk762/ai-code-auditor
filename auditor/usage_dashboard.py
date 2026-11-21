"""Aggregations for the API usage dashboard (GET /audit/usage, /audit/usage.json).

Everything is computed from the spend ledger in local time, so "hungry periods" line up with
the timers (00:30 conductor, 01:30 batch review, hourly merge poll).

The JSON also carries a status line for pollers (``usage_status``), in the same vocabulary as
mithrandir's ``LATEST.json`` envelope: ``summary`` (one line, at most 200 chars), ``spoken``
(TTS-safe, like ``prs.json``'s) and ``severity`` (info | notice | warning | critical). Severity
uses the dashboard's thresholds: ``notice`` at 85% of a cap (projected total, or a lane spent),
``warning`` when the total is projected over its cap or a lane has hit its cap (that lane's calls
are refused), ``critical`` when the total cap is reached (every capped call is refused).
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from auditor.spend_ledger import budget_month_start, get_ledger

HTML_PATH = Path(__file__).parent / "static" / "usage_dashboard.html"
TOP_PROCESSES = 12
ONE_OFF_PREFIX = "baseline:"
NEAR_CAP = 0.85  # same threshold as the dashboard's "Close to cap"
SUMMARY_MAX = 200


def _next_reset(start: date) -> date:
    month = start.month % 12 + 1
    year = start.year + (1 if start.month == 12 else 0)
    # Clamp to the target month's length (Jan 31 -> Feb 28/29).
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _recurring(row: tuple) -> bool:
    return row[2] != "manual" and not str(row[3]).startswith(ONE_OFF_PREFIX)


def usage_data(range_key: str = "month") -> dict[str, Any]:
    ledger = get_ledger()
    now = datetime.now()
    today = now.date()
    m_start = budget_month_start(today)
    m_reset = _next_reset(m_start)

    if range_key == "month":
        r_start = m_start
    else:
        try:
            days = max(1, min(int(str(range_key).rstrip("d") or 14), 90))
        except ValueError:  # "week", "1.5d", a stale URL: the default window, not a 500
            days = 14
        r_start = today - timedelta(days=days - 1)
    since = min(r_start, m_start)
    rows = ledger.rows_since(datetime(since.year, since.month, since.day).timestamp())

    month_spent = sum(r[7] for r in rows if datetime.fromtimestamp(r[0]).date() >= m_start)
    in_range = [r for r in rows if datetime.fromtimestamp(r[0]).date() >= r_start]

    # Burn rate from the last 7 days (or since month start if shorter), counting only recurring
    # spend: manual entries (evals) and one-offs (note "baseline:…", e.g. a full-repo Opus sweep)
    # still count against caps but would wildly inflate the projection.
    burn_from = max(m_start, today - timedelta(days=6))
    burn_days = (today - burn_from).days + 1
    burn = sum(r[7] for r in rows if _recurring(r)
               and datetime.fromtimestamp(r[0]).date() >= burn_from) / burn_days
    one_off = sum(r[7] for r in rows if not _recurring(r)
                  and datetime.fromtimestamp(r[0]).date() >= m_start)
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

    out = {
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
            "one_off": round(one_off, 4),
            # Same definition as the chart's last point: today is already in month_spent, so
            # only the full days after it are projected.
            "projected": round(month_spent + burn * max(days_left - 1, 0), 4),
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
    out.update(usage_status(out))
    return out


def _dollars(usd: float) -> str:
    """Spoken money: whole dollars, no symbols."""
    if 0 < usd < 1:
        return "under a dollar"
    n = round(usd)
    return f"{n} dollar{'' if n == 1 else 's'}"


def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def usage_status(data: dict[str, Any]) -> dict[str, str]:
    """``summary``, ``spoken`` and ``severity`` for the budget month in ``usage_data``'s output."""
    m = data["month"]
    spent, cap, projected, days_left = m["spent"], m["cap"], m["projected"], m["days_left"]
    capped_lanes = [ln for ln in data.get("lanes") or [] if ln.get("cap")]
    at_cap = [ln["lane"] for ln in capped_lanes if ln["month_spent"] >= ln["cap"]]
    near = [ln["lane"] for ln in capped_lanes
            if NEAR_CAP * ln["cap"] <= ln["month_spent"] < ln["cap"]]

    if cap and spent >= cap:
        severity = "critical"
    elif (cap and projected >= cap) or at_cap:
        severity = "warning"
    elif (cap and projected >= NEAR_CAP * cap) or near:
        severity = "notice"
    else:
        severity = "info"

    if cap:
        head = f"Claude API spend this month: ${spent:.2f} of ${cap:g} cap ({spent / cap:.0%})"
    else:
        head = f"Claude API spend this month: ${spent:.2f} (no cap)"
    lanes = ""
    if at_cap:
        lanes = f"; at lane cap: {', '.join(at_cap)}"
    elif near:
        lanes = f"; near lane cap: {', '.join(near)}"
    summary = f"{head}, projected ${projected:.2f}; resets in {_days(days_left)}{lanes}"
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX - 3].rstrip() + "..."

    if cap:
        pct = round(100 * spent / cap)
        parts = [f"Claude API spend this month is {_dollars(spent)} of a {round(cap)} dollar cap, {pct} percent."]
    else:
        parts = [f"Claude API spend this month is {_dollars(spent)}, with no cap."]
    if cap and spent >= cap:
        parts.append("The monthly cap is reached, so capped calls are refused.")
    elif cap and projected >= cap:
        parts.append(f"At this rate it goes over the cap, reaching {_dollars(projected)}.")
    else:
        parts.append(f"At this rate it reaches {_dollars(projected)}.")
    spoken_lanes = [ln.replace("_", " ") for ln in at_cap]
    if len(spoken_lanes) == 1:
        parts.append(f"The {spoken_lanes[0]} lane has hit its cap.")
    elif spoken_lanes:
        parts.append(f"The {', '.join(spoken_lanes[:-1])} and {spoken_lanes[-1]} lanes have hit their caps.")
    parts.append(f"The budget resets in {_days(days_left)}.")
    return {"summary": summary, "spoken": " ".join(parts), "severity": severity}
