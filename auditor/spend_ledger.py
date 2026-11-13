"""Monthly API spend ledger with per-lane caps.

Every Claude call records its real token usage here (sync, batch, conductor). Before a call,
callers ask `check(lane)`; once a lane's cap or the overall cap for the current budget month is
reached it raises BudgetExceeded, and the caller degrades (audit → heuristics/skip, batch → submit
only what fits, conductor → task fails with the reason).

Budget month starts on AI_AUDIT_BUDGET_RESET_DAY (default 1) — set it to the day your API
credit renews. Caps (USD, per budget month):
  AI_AUDIT_BUDGET_TOTAL_USD        overall, default 190
  AI_AUDIT_BUDGET_<LANE>_USD       per lane: AUDIT (merge/sync audits, default 15),
                                   AUDIT_BATCH (nightly Opus batch, default 100),
                                   CONDUCTOR (default 80); unset/empty = no lane cap
Storage: AI_AUDIT_SPEND_DB (default index/llm_spend.db — shared with the audit-api container).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

# $ per 1M tokens: (input, output). Cache writes bill at 1.25x input, reads at 0.1x.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-fable-5-2": (8.00, 40.00),
}
DEFAULT_LANE_CAPS = {"audit": 15.0, "audit_batch": 100.0, "conductor": 80.0}
DEFAULT_PATH = "index/llm_spend.db"


class BudgetExceeded(RuntimeError):
    pass


def bare_model(model: str) -> str:
    return model.split("/", 1)[-1]


def cost_usd(model: str, input_tokens: int, output_tokens: int, *, cache_write: int = 0,
             cache_read: int = 0, batch: bool = False) -> float:
    pin, pout = PRICES.get(bare_model(model), (0.0, 0.0))
    usd = (input_tokens * pin + cache_write * pin * 1.25 + cache_read * pin * 0.1
           + output_tokens * pout) / 1e6
    return usd * 0.5 if batch else usd


def budget_month_start(today: date | None = None, reset_day: int | None = None) -> date:
    today = today or date.today()
    day = reset_day or int(os.getenv("AI_AUDIT_BUDGET_RESET_DAY", "1"))
    day = max(1, min(day, 28))
    if today.day >= day:
        return today.replace(day=day)
    prev_month = today.month - 1 or 12
    prev_year = today.year if today.month > 1 else today.year - 1
    return date(prev_year, prev_month, day)


def _cap(name: str, default: float | None) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return float(raw) if raw else None


class SpendLedger:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.getenv("AI_AUDIT_SPEND_DB", DEFAULT_PATH))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS spend (
                   ts REAL NOT NULL, lane TEXT NOT NULL, model TEXT NOT NULL,
                   input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                   cache_write INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
                   batch INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL, note TEXT DEFAULT ''
               )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS spend_ts ON spend(ts)")
        self._conn.commit()

    # ── recording ────────────────────────────────────────────────────────────
    def record(self, lane: str, model: str, input_tokens: int, output_tokens: int, *,
               cache_write: int = 0, cache_read: int = 0, batch: bool = False,
               note: str = "") -> float:
        usd = cost_usd(model, input_tokens, output_tokens, cache_write=cache_write,
                       cache_read=cache_read, batch=batch)
        with self._lock:
            self._conn.execute(
                "INSERT INTO spend VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), lane, bare_model(model), input_tokens, output_tokens,
                 cache_write, cache_read, int(batch), usd, note),
            )
            self._conn.commit()
        return usd

    def record_usage(self, lane: str, model: str, usage: Any, *, batch: bool = False,
                     note: str = "") -> float:
        """Record from an API usage object or dict (Anthropic field names)."""
        get = usage.get if isinstance(usage, dict) else (lambda k, d=0: getattr(usage, k, d))
        return self.record(
            lane, model, int(get("input_tokens", 0) or 0), int(get("output_tokens", 0) or 0),
            cache_write=int(get("cache_creation_input_tokens", 0) or 0),
            cache_read=int(get("cache_read_input_tokens", 0) or 0), batch=batch, note=note,
        )

    def record_manual(self, lane: str, usd: float, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO spend VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), lane, "manual", 0, 0, 0, 0, 0, usd, note),
            )
            self._conn.commit()

    # ── querying ─────────────────────────────────────────────────────────────
    def _since(self) -> float:
        start = budget_month_start()
        return datetime(start.year, start.month, start.day).timestamp()

    def spent(self, lane: str | None = None) -> float:
        q, args = "SELECT COALESCE(SUM(cost_usd),0) FROM spend WHERE ts >= ?", [self._since()]
        if lane:
            q += " AND lane = ?"
            args.append(lane)
        with self._lock:
            return float(self._conn.execute(q, args).fetchone()[0])

    def caps(self) -> dict[str, float | None]:
        caps = {"total": _cap("AI_AUDIT_BUDGET_TOTAL_USD", 190.0)}
        for lane, default in DEFAULT_LANE_CAPS.items():
            caps[lane] = _cap(f"AI_AUDIT_BUDGET_{lane.upper()}_USD", default)
        return caps

    def remaining(self, lane: str) -> float:
        """USD left for this lane: the tighter of its own cap and the overall cap."""
        caps = self.caps()
        left = []
        if caps.get("total") is not None:
            left.append(caps["total"] - self.spent())
        if caps.get(lane) is not None:
            left.append(caps[lane] - self.spent(lane))
        return min(left) if left else float("inf")

    def check(self, lane: str) -> None:
        if self.remaining(lane) <= 0:
            raise BudgetExceeded(
                f"monthly budget reached for lane '{lane}' "
                f"(lane ${self.spent(lane):.2f}, total ${self.spent():.2f}; caps {self.caps()})"
            )

    def summary(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT lane, model, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) "
                "FROM spend WHERE ts >= ? GROUP BY lane, model ORDER BY SUM(cost_usd) DESC",
                (self._since(),),
            ).fetchall()
        caps = self.caps()
        lanes = sorted({r[0] for r in rows} | set(DEFAULT_LANE_CAPS))
        return {
            "month_start": budget_month_start().isoformat(),
            "total_spent": round(self.spent(), 4),
            "total_cap": caps["total"],
            "lanes": {
                lane: {"spent": round(self.spent(lane), 4), "cap": caps.get(lane),
                       "remaining": round(self.remaining(lane), 4)}
                for lane in lanes
            },
            "by_model": [
                {"lane": r[0], "model": r[1], "calls": r[2], "input_tokens": r[3],
                 "output_tokens": r[4], "cost": round(r[5], 4)}
                for r in rows
            ],
        }


_LEDGER: SpendLedger | None = None
_LEDGER_LOCK = threading.Lock()


def get_ledger() -> SpendLedger:
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None or str(_LEDGER.path) != os.getenv("AI_AUDIT_SPEND_DB", str(_LEDGER.path)):
            _LEDGER = SpendLedger()
        return _LEDGER
