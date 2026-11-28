"""Reliability gates for the trust upgrade (#63): what "provably reliable" means, as code.

Each gate names a component, a measured value, a threshold, and a minimum sample. Below the
minimum the gate is ``insufficient``, not passed. A gate whose component hasn't been built yet
is ``pending`` (none today). An *informational* gate (``INFORMATIONAL``) is measured and shown
like any other but kept out of the exit criteria. ``evaluate()`` writes ``reports/trust_eval.json`` and appends a line to
``trust_eval_history.jsonl``. The exit criteria for epic #64 are computed from that history:

- every gate that isn't pending or informational passed in each of two consecutive ISO weeks (the
  week's last evaluation counts), and none of those is insufficient;
- the headline trust metrics are stable or improving over the last 4 weekly snapshots of
  ``trust_history.jsonl``.

Gates read the latest trust report (#52), the seeded-defect eval of the evidence bar
(``eval_seeded``) and the seeded recall of class guards (``guard_seeded``), so evaluating is cheap
and runs after every nightly repo audit. Both seeded evals are slow (they run real test suites)
and refresh weekly.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPORT_NAME = "trust_eval.json"
HISTORY_NAME = "trust_eval_history.jsonl"
SEEDED_NAME = "eval_seeded.json"
GUARD_SEEDED_NAME = "guard_seeded.json"
SEEDED_MAX_AGE = timedelta(days=8)  # a weekly refresh plus slack; older results don't count


# Measured and shown, but not part of the exit criteria, with the reason.
INFORMATIONAL: dict[str, str] = {
    # Rule-attributed closures (#126) are rare by nature: most findings are semantic, and on
    # 2026-11-22 only 12 of 244 open children in ruff-expressible classes tied to any ruff hit,
    # mostly weakly. 10 closures isn't reachable soon. Guards are judged by
    # guards.seeded_recall instead (#63): every planted instance of a guarded class caught.
    "class_packs.lifecycle": "informational: guards are judged by seeded recall (#63)",
    # Variants of the seeds (#155): new, with few of them, so measured before they decide anything.
    "guards.dodge_recall": "informational until measured (#155)",
    "guards.fix_silence": "informational until measured (#155)",
    # The write-time guard hook (auditor/guard_hook.py): advice to the agent while it edits, never
    # evidence. Measured to see whether it changes what reaches the nightly scan before it decides anything.
    "guards.write_time": "informational: the write-time hook is advice, never evidence",
}


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
    informational: bool = False  # measured and shown, kept out of the exit criteria

    def judge(self, value: float | None, n: int, note: str = "") -> Gate:
        self.value, self.n, self.note = value, n, note
        if value is None or n < self.min_n:
            self.status = "insufficient"
        else:
            ok = value <= self.threshold if self.op == "<=" else value >= self.threshold
            self.status = "pass" if ok else "fail"
        return self


def _since_bar(report: dict[str, Any]) -> tuple[int, int, int, str]:
    """(resolutions the evidence gates judge, of them lint-only, of them waived, scope note).

    The trust report's rolling window when it has one, else every resolution under the bar."""
    fr = report.get("false_resolve") or {}
    w = fr.get("window")
    if w:
        return int(w.get("resolutions") or 0), int(w.get("lint_only") or 0), int(w.get("waived") or 0), \
            f"last {w.get('days')} days"
    by = fr.get("by_evidence") or {}
    return sum(by.values()), int(fr.get("lint_only_since_bar") or 0), int(by.get("waiver") or 0), "since the bar"


def build_gates(trust: dict[str, Any] | None, seeded: dict[str, Any] | None,
                classes: dict[str, Any] | None = None, class_lifecycle: dict[str, Any] | None = None,
                rules_fixtures: dict[str, Any] | None = None,
                arch_contracts: dict[str, Any] | None = None,
                guard_seeded: dict[str, Any] | None = None,
                guard_hook: dict[str, Any] | None = None) -> list[Gate]:
    t = trust or {}
    gates: list[Gate] = []

    # ── #58 evidence bar ──
    n, lint, waived, scope = _since_bar(t)
    gates.append(Gate("evidence.lint_only", "evidence bar", 58,
                      f"Resolutions under the evidence bar where no tests ran ({scope})", "<=", 0.05, 20)
                 .judge(lint / n if n else None, n))
    gates.append(Gate("evidence.waiver_rate", "evidence bar", 58,
                      f"Resolutions under the evidence bar that were waived, not proven ({scope})", "<=", 0.20, 20)
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

    # ── #53 finding classes: measured on the blind, held-out gold set only ──
    test = ((classes or {}).get("sets") or {}).get("test") or {}
    none = "" if classes else "no gold set (~/.config/ai-code-auditor/eval/finding_classes_*.jsonl)"
    gates.append(Gate("classes.precision", "finding classes", 53,
                      "Classifier precision on the held-out gold set (labelled blind, council-checked)", ">=", 0.95, 30)
                 .judge(test.get("precision"), int(test.get("classified") or 0), none))
    gates.append(Gate("classes.coverage", "finding classes", 53,
                      "Held-out findings the classifier assigns a class (logic_error rows included)", ">=", 0.80, 50)
                 .judge(test.get("coverage"), int(test.get("n") or 0), none))

    # ── #54 class packs ──
    cl = class_lifecycle or {}
    gates.append(Gate("class_packs.lifecycle", "class packs", 54,
                      "Findings a proven class guard closed that stayed closed (no reopen, no recurrence)", ">=", 0.95, 10)
                 .judge(cl.get("rate"), int(cl.get("n") or 0)))

    # ── #55 suite rules ──
    rf = rules_fixtures or {}
    gates.append(Gate("rules.fixtures", "suite rules", 55,
                      "Every suite rule fires on its positive fixture and not its negative", ">=", 1.0, 5)
                 .judge(rf.get("rate"), int(rf.get("n") or 0),
                        "" if rules_fixtures else "no suite-rules catalogue (argus-ci suite-rules/ on dev)"))

    # ── #57 architecture contracts ──
    ac = arch_contracts or {}
    failing = [r["rule"] for r in ac.get("rules") or [] if not r.get("fired")]
    note = ac.get("error") or ("no architecture rules" if not ac.get("n") else
                               f"not firing: {', '.join(failing)}" if failing else "")
    gates.append(Gate("arch.contracts", "architecture contracts", 57,
                      "Every architecture rule fires once on its seeded violation and never on a clean graph",
                      ">=", 1.0, 1)
                 .judge(ac.get("rate"), int(ac.get("n") or 0), note))

    # ── #63 seeded recall of proven class guards ──
    from auditor.guard_seeded import recall_metric, variant_metric

    gs = guard_seeded or {}
    gs_fresh = bool(gs) and _age(gs.get("at")) <= SEEDED_MAX_AGE
    rm = recall_metric(gs if gs_fresh else None)
    gs_note = ("no guard seeded eval (guard_seeded.json)" if not gs else
               f"guard seeded eval is {_age(gs.get('at')).days} days old" if not gs_fresh else rm["note"])
    gates.append(Gate("guards.seeded_recall", "class packs", 63,
                      "Seeded instances of a guarded class that its proven guard's rule or proof test catches",
                      ">=", 1.0, max(2, 2 * rm["guards"]))
                 .judge(rm["rate"], rm["n"], gs_note))
    # ── #155 variants of those seeds (informational) ──
    for gid, kind, desc in (
            ("guards.dodge_recall", "dodges", "Rewritten instances of a guarded class that its guard still catches"),
            ("guards.fix_silence", "fixes", "Real fixes of a guarded class that its guard stays silent on")):
        vm = variant_metric(gs if gs_fresh else None, kind)
        gates.append(Gate(gid, "class packs", 155, desc, ">=", 1.0, 5)
                     .judge(vm["rate"], vm["n"], vm["note"] if gs_fresh else gs_note))
    # ── write-time guard hook (informational) ──
    gh = guard_hook or {}
    gates.append(Gate("guards.write_time", "guard hook", 64,
                      "Guard-rule hits shown to an agent at write time that are gone by its next check of the file",
                      ">=", 0.8, 20)
                 .judge(gh.get("rate"), int(gh.get("n") or 0),
                        "" if gh.get("n") else "no write-time hook history (guard_hook_history.jsonl)"))
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


def _monday(at: Any) -> date:
    d = at if isinstance(at, datetime) else datetime.fromisoformat(str(at))
    return d.date() - timedelta(days=d.isocalendar()[2] - 1)


def gates_exit(history: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Every non-pending gate passed (none insufficient) in each of the last two ISO weeks.

    The two weeks must be consecutive and the newer one the current or previous ISO week: a week
    with no evaluation is not green, so a gap in the history breaks the run."""
    weeks = weekly_last(history)
    def green(row: dict[str, Any]) -> bool:
        st = row.get("statuses") or {}
        live = [v for k, v in st.items() if v != "pending" and k not in INFORMATIONAL]
        return bool(live) and all(v == "pass" for v in live)
    last2 = weeks[-2:]
    adjacent = (len(last2) == 2 and _monday(last2[1]["at"]) - _monday(last2[0]["at"]) == timedelta(weeks=1)
                and _monday(last2[1]["at"]) >= _monday(now or datetime.now(UTC)) - timedelta(weeks=1))
    return {"met": adjacent and all(green(r) for r in last2),
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

def classes_eval(out: Path) -> dict[str, Any] | None:
    """Score the classifier on the private gold set (cheap: no model calls); None without one."""
    from auditor import finding_class, finding_class_review

    try:
        rows = finding_class_review.load_gold()
    except (OSError, ValueError) as exc:
        logger.warning("gold set unreadable: %s", exc)
        return None
    if not rows:
        return None
    from auditor.finding_class_llm import consensus_predictor

    # The gate scores the classifier packs actually get: all-three-models consensus, from stored
    # answers (no API call). Until those exist, the title rules.
    cp = consensus_predictor(out)
    rep = finding_class.evaluate(rows, predict=cp)
    rep["classifier"] = "consensus" if cp else "rules"
    (out / finding_class.EVAL_NAME).write_text(json.dumps(rep, indent=1))
    return rep


def class_lifecycle_eval(out: Path) -> dict[str, Any] | None:
    from auditor.class_packs import lifecycle_metric

    try:
        return lifecycle_metric(out)
    except (OSError, ValueError) as exc:
        logger.warning("class pack lifecycle unreadable: %s", exc)
        return None


def guard_hook_eval(out: Path) -> dict[str, Any] | None:
    from auditor.guard_hook import write_time_metric

    try:
        return write_time_metric(out)
    except (OSError, ValueError) as exc:
        logger.warning("guard hook history unreadable: %s", exc)
        return None


def arch_contracts_eval(rules_path: str | Path = "config/architecture_rules.yaml") -> dict[str, Any]:
    """Every architecture rule against its seeded fixture (cheap: no scan). A rules file that fails
    to load is reported as an error with no rules, so the gate is insufficient, never passing."""
    from auditor.config import load_architecture_rules
    from ecosystem import arch_contracts

    try:
        return arch_contracts.fixtures(load_architecture_rules(rules_path))
    except (OSError, ValueError) as exc:
        logger.warning("architecture rules unreadable: %s", exc)
        return {"n": 0, "fired": 0, "rate": None, "rules": [], "error": str(exc)[:500]}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        # bad bytes only spoil their own line, which the per-line parse then skips
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def evaluate(output_dir: str | Path | None = None, *, record: bool = True,
             now: datetime | None = None) -> dict[str, Any]:
    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    from auditor.trust_metrics import HISTORY_NAME as TRUST_HISTORY
    from auditor.trust_metrics import REPORT_NAME as TRUST_REPORT

    from auditor.suite_rules import fixtures as rules_fixtures

    gates = build_gates(_read_json(out / TRUST_REPORT), _read_json(out / SEEDED_NAME), classes_eval(out),
                        class_lifecycle_eval(out), rules_fixtures(out), arch_contracts_eval(),
                        _read_json(out / GUARD_SEEDED_NAME), guard_hook_eval(out))
    for g in gates:
        if g.id in INFORMATIONAL:
            g.informational = True
            g.note = "; ".join(x for x in (g.note, INFORMATIONAL[g.id]) if x)
    at = (now or datetime.now(UTC)).isoformat()
    statuses = {g.id: g.status for g in gates}
    history = [*_read_jsonl(out / HISTORY_NAME), {"at": at, "statuses": statuses}]
    report = {
        "at": at,
        "gates": [asdict(g) for g in gates],
        "summary": {**{s: sum(1 for g in gates if g.status == s and not g.informational)
                       for s in ("pass", "fail", "insufficient", "pending")},
                    "informational": sum(1 for g in gates if g.informational)},
        "exit": {
            "gates_two_weeks": gates_exit(history, now=datetime.fromisoformat(at)),
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
                     guard_runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Pipeline tail: refresh each seeded eval when it's a week old, then evaluate. Never raises."""
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
        gs = _read_json(out / GUARD_SEEDED_NAME)
        if gs is None or _age(gs.get("at")) > timedelta(days=7):
            from auditor import guard_seeded

            (guard_runner or guard_seeded.run)(out)
    except Exception as exc:  # noqa: BLE001 — the gate reports stale guard results as insufficient
        logger.warning("guard seeded eval failed: %s", exc)
    try:
        return evaluate(out)["summary"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("trust eval failed: %s", exc)
        return {"error": str(exc)[:300]}
