"""Reliability gates for the trust upgrade (#63): what "provably reliable" means, as code.

Each gate names a component, a measured value, a threshold, and a minimum sample. Below the
minimum the gate is ``insufficient``, not passed. A gate whose component hasn't been built yet
is ``pending``. ``evaluate()`` writes ``reports/trust_eval.json`` and appends a line to
``trust_eval_history.jsonl``. The exit criteria for epic #64 are computed from that history:

- every gate that isn't pending passed in each of two consecutive ISO weeks (the week's last
  evaluation counts), and nothing is insufficient;
- the headline trust metrics are stable or improving over the last 4 weekly snapshots of
  ``trust_history.jsonl``.

Gates read the latest trust report (#52) and the seeded-defect eval (``eval_seeded``), so
evaluating is cheap and runs after every nightly repo audit. The seeded eval is slow (it runs
real test suites) and refreshes weekly.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPORT_NAME = "trust_eval.json"
HISTORY_NAME = "trust_eval_history.jsonl"
SEEDED_NAME = "eval_seeded.json"
SEEDED_MAX_AGE = timedelta(days=8)  # a weekly refresh plus slack; older results don't count


@dataclass
class Gate:
    id: str
    component: str
    issue: int
    description: str
    op: str             # "<=" or ">="
    threshold: float
    min_n: int
    value: float | None = None
    n: int = 0
    status: str = "pending"  # pass | fail | insufficient | pending
    note: str = ""

    def judge(self, value: float | None, n: int, note: str = "") -> Gate:
        self.value, self.n, self.note = value, n, note
        if value is None or n < self.min_n:
            self.status = "insufficient"
        else:
            ok = value <= self.threshold if self.op == "<=" else value >= self.threshold
            self.status = "pass" if ok else "fail"
        return self


def _since_bar(report: dict[str, Any]) -> tuple[int, int, int]:
    """(resolutions under the evidence bar, of them lint-only, of them waived)."""
    fr = report.get("false_resolve") or {}
    by = fr.get("by_evidence") or {}
    n = sum(by.values())
    return n, int(fr.get("lint_only_since_bar") or 0), int(by.get("waiver") or 0)


def build_gates(trust: dict[str, Any] | None, seeded: dict[str, Any] | None) -> list[Gate]:
    t = trust or {}
    gates: list[Gate] = []

    # ── #58 evidence bar ──
    n, lint, waived = _since_bar(t)
    gates.append(Gate("evidence.lint_only", "evidence bar", 58,
                      "Resolutions under the evidence bar where no tests ran", "<=", 0.05, 20)
                 .judge(lint / n if n else None, n))
    gates.append(Gate("evidence.waiver_rate", "evidence bar", 58,
                      "Resolutions under the evidence bar that were waived, not proven", "<=", 0.20, 20)
                 .judge(waived / n if n else None, n))
    s = seeded or {}
    fresh = bool(s) and _age(s.get("at")) <= SEEDED_MAX_AGE
    stale = "" if fresh or not s else f"seeded eval is {(_age(s.get('at'))).days} days old"
    real = s.get("scenarios") or []
    bugs = [x for x in real if x.get("case") == "fix"]
    controls = [x for x in real if x.get("case") == "control"]
    gates.append(Gate("evidence.seeded_recall", "evidence bar", 58,
                      "Seeded bugs in real repos whose real test the bar finds as the reproduction", ">=", 1.0, 5)
                 .judge(sum(x["detected"] for x in bugs) / len(bugs) if bugs and fresh else None,
                        len(bugs) if fresh else 0, stale))
    gates.append(Gate("evidence.seeded_false_proof", "evidence bar", 58,
                      "Cosmetic non-fixes the bar wrongly accepts as proven", "<=", 0.0, 5)
                 .judge(sum(x["detected"] for x in controls) / len(controls) if controls and fresh else None,
                        len(controls) if fresh else 0, stale))

    # ── #52 trust metrics ──
    rc = t.get("recurrence") or {}
    gates.append(Gate("trust.regressed", "trust metrics", 52,
                      "Resolved packs whose issue came back (regressions)", "<=", 0.05, 50)
                 .judge(rc.get("rate"), int(rc.get("observed") or 0)))
    prs = t.get("agent_prs") or {}
    gates.append(Gate("trust.reverted", "trust metrics", 52,
                      "Agent fix PRs reverted within 14 days (mature PRs only)", "<=", 0.02, 20)
                 .judge(prs.get("mature_rate"), int(prs.get("mature") or 0)))
    sp = t.get("scan_persistence") or {}
    gates.append(Gate("scan.persistence", "scanner", 52,
                      "Findings the next scan of unchanged code reports again", ">=", 0.85, 500)
                 .judge(sp.get("rate"), int(sp.get("findings") or 0)))

    # ── #67 pack GC ──
    g = t.get("gc") or {}
    gates.append(Gate("gc.reopen_rate", "pack GC", 67,
                      "Packs GC closed as obsolete that a scan then reported again", "<=", 0.10, 20)
                 .judge(g.get("rate"), int(g.get("closed") or 0)))

    # ── not built yet: visible, never counted as passing ──
    for gid, comp, issue, desc in (
        ("classes.precision", "finding classes", 53, "Rule classifier precision on the held-out gold set"),
        ("class_packs.lifecycle", "class packs", 54, "Class-guard packs close children only when the guard holds"),
        ("rules.fixtures", "suite rules", 55, "Every suite rule fires on its positive fixture and not its negative"),
        ("arch.contracts", "architecture contracts", 57, "Every architecture contract fires on a seeded violation"),
    ):
        gates.append(Gate(gid, comp, issue, desc, ">=", 1.0, 1, note="not built yet"))
    return gates


def _age(at: Any) -> timedelta:
    try:
        when = datetime.fromisoformat(str(at))
    except ValueError:
        return timedelta.max
    return datetime.now(UTC) - (when if when.tzinfo else when.replace(tzinfo=UTC))


# ── exit criteria ────────────────────────────────────────────────────────────

def _week(at: str) -> str:
    y, w, _ = datetime.fromisoformat(at).isocalendar()
    return f"{y}-W{w:02d}"


def weekly_last(rows: list[dict[str, Any]], key: str = "at") -> list[dict[str, Any]]:
    """The last row of each ISO week, oldest first."""
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            by[_week(str(r[key]))] = r
        except (KeyError, ValueError):
            continue
    return [by[w] for w in sorted(by)]


def gates_exit(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Every non-pending gate passed (none insufficient) in each of the last two ISO weeks."""
    weeks = weekly_last(history)
    def green(row: dict[str, Any]) -> bool:
        st = row.get("statuses") or {}
        live = [v for v in st.values() if v != "pending"]
        return bool(live) and all(v == "pass" for v in live)
    last2 = weeks[-2:]
    return {"met": len(last2) == 2 and all(green(r) for r in last2),
            "weeks": [{"week": _week(r["at"]), "green": green(r)} for r in weeks[-6:]]}


# Headline metrics where lower is better / higher is better; slack absorbs noise between snapshots.
_LOWER = ("false_resolve_rate", "recurrence_rate", "revert_rate", "reopen_rate", "gc_reopen_rate")
_HIGHER = ("scan_persistence",)
_SLACK = 0.02


def metrics_exit(trust_history: list[dict[str, Any]]) -> dict[str, Any]:
    """The headline trust metrics didn't get worse (beyond slack) across the last 4 weekly snapshots."""
    weeks = weekly_last(trust_history)[-4:]
    if len(weeks) < 4:
        return {"met": False, "weeks": len(weeks), "worse": [], "note": f"{len(weeks)} of 4 weekly snapshots"}
    worse = []
    for k in _LOWER + _HIGHER:
        vals = [w.get(k) for w in weeks if w.get(k) is not None]
        if len(vals) < 2:
            continue
        if (k in _LOWER and vals[-1] > vals[0] + _SLACK) or (k in _HIGHER and vals[-1] < vals[0] - _SLACK):
            worse.append({"metric": k, "from": vals[0], "to": vals[-1]})
    return {"met": not worse, "weeks": 4, "worse": worse}


# ── run ──────────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def evaluate(output_dir: str | Path | None = None, *, record: bool = True,
             now: datetime | None = None) -> dict[str, Any]:
    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    from auditor.trust_metrics import HISTORY_NAME as TRUST_HISTORY
    from auditor.trust_metrics import REPORT_NAME as TRUST_REPORT

    gates = build_gates(_read_json(out / TRUST_REPORT), _read_json(out / SEEDED_NAME))
    at = (now or datetime.now(UTC)).isoformat()
    statuses = {g.id: g.status for g in gates}
    history = [*_read_jsonl(out / HISTORY_NAME), {"at": at, "statuses": statuses}]
    report = {
        "at": at,
        "gates": [asdict(g) for g in gates],
        "summary": {s: sum(1 for g in gates if g.status == s) for s in ("pass", "fail", "insufficient", "pending")},
        "exit": {
            "gates_two_weeks": gates_exit(history),
            "metrics_four_weeks": metrics_exit(_read_jsonl(out / TRUST_HISTORY)),
        },
    }
    report["exit"]["met"] = (report["exit"]["gates_two_weeks"]["met"] and report["exit"]["metrics_four_weeks"]["met"]
                             and report["summary"]["pending"] == 0)
    if record:
        (out / REPORT_NAME).write_text(json.dumps(report, indent=1))
        with (out / HISTORY_NAME).open("a") as fh:
            fh.write(json.dumps({"at": at, "statuses": statuses}) + "\n")
    return report


def evaluate_quietly(output_dir: str | Path | None = None, *, seeded_runner: Callable[..., Any] | None = None,
                     ) -> dict[str, Any]:
    """Pipeline tail: refresh the seeded eval when it's a week old, then evaluate. Never raises."""
    if os.getenv("AI_AUDIT_TRUST_EVAL", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {"skipped": "AI_AUDIT_TRUST_EVAL=0"}
    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    try:
        s = _read_json(out / SEEDED_NAME)
        if s is None or _age(s.get("at")) > timedelta(days=7):
            from auditor import eval_seeded

            (seeded_runner or eval_seeded.run)(out)
    except Exception as exc:  # noqa: BLE001 — the gates report a stale seeded eval as insufficient
        logger.warning("seeded eval failed: %s", exc)
    try:
        return evaluate(out)["summary"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("trust eval failed: %s", exc)
        return {"error": str(exc)[:300]}
