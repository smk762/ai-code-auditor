"""Trust-gated autonomy (#61): the conductor's scope grows only where measured trust allows.

Each (repo, finding class) scope has a level: ``report`` (findings only), ``draft_pr``,
``ready_pr``, and later ``auto_merge``. Every scope starts at the config's ``default``.
``refresh`` runs nightly after the trust metrics and moves each scope at most one level:

- **Demote** when any threshold is breached over the scope's window (``trust_metrics.scope_metric``:
  its last ``window_prs`` mature agent PRs): revert, recurrence or false-resolve rate above its
  limit. Any breach, however small the sample: one revert is a signal, not noise. Never below
  ``floor`` (``draft_pr``): a draft PR already waits for a person, so ``report`` would stop the
  PRs without making anything safer. A scope goes to ``report`` only by an override.
- **Promote** when the window is full, no threshold is breached, every pack in it was resolved
  with validate_patch evidence (no lint-only, no waiver), the scope has held its level for
  ``min_days_at_level``, and the next level is within ``ceiling``.

``decide`` is what the conductor asks before opening a PR. It caps a change touching an
irreversible area (auth, secrets, migrations, data deletion: ``irreversible`` in the config) at
``draft_pr``, whatever the metrics say, and applies the config's ``overrides``.

State is ``reports/autonomy.json`` (levels, since when, why, the metrics). Each refresh appends one
line to ``reports/autonomy_history.jsonl``, which is never overwritten: it is the record of a month
on ``draft_pr`` that any promotion needs.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from auditor.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

LEVELS = ("report", "draft_pr", "ready_pr", "auto_merge")
THRESHOLDS = ("revert_rate", "recurrence_rate", "false_resolve_rate")
CONFIG_PATH = "config/autonomy.yaml"
STATE_NAME = "autonomy.json"
HISTORY_NAME = "autonomy_history.jsonl"
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Config:
    default: str = "draft_pr"
    floor: str = "draft_pr"
    ceiling: str = "ready_pr"
    window_prs: int = 20
    min_days_at_level: int = 30
    thresholds: dict[str, float] = field(default_factory=lambda: {
        "revert_rate": 0.02, "recurrence_rate": 0.05, "false_resolve_rate": 0.05})
    irreversible_classes: frozenset[str] = frozenset()
    irreversible_paths: tuple[str, ...] = ()
    irreversible_titles: tuple[re.Pattern[str], ...] = ()
    overrides: tuple[dict[str, str], ...] = ()


def _rank(level: str) -> int:
    return LEVELS.index(level)


def load_config(path: str | Path | None = None) -> Config:
    """Read and validate the config (``AI_AUDIT_AUTONOMY_CONFIG``, else ``config/autonomy.yaml``).
    A missing file is the defaults; a bad one raises ValueError."""
    path = path or os.getenv("AI_AUDIT_AUTONOMY_CONFIG", "").strip() or CONFIG_PATH
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return Config()
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")  # noqa: TRY004 — bad config data, not a bad call
    errors: list[str] = []

    def level(key: str, fallback: str) -> str:
        v = str(raw.get(key, fallback))
        if v not in LEVELS:
            errors.append(f"{key}: {v!r} is not one of {LEVELS}")
            return fallback
        return v

    default, ceiling = level("default", "draft_pr"), level("ceiling", "ready_pr")
    floor = level("floor", "draft_pr")
    if not _rank(floor) <= _rank(default) <= _rank(ceiling):
        errors.append(f"need floor <= default <= ceiling, got {floor!r}, {default!r}, {ceiling!r}")
    th = {**Config().thresholds, **(raw.get("thresholds") or {})}
    for k, v in th.items():
        if k not in THRESHOLDS:
            errors.append(f"thresholds: unknown {k!r} (known: {THRESHOLDS})")
        elif not isinstance(v, (int, float)) or not 0 <= v <= 1:
            errors.append(f"thresholds.{k}: must be a rate between 0 and 1")
    irr = raw.get("irreversible") or {}
    titles = []
    for pat in irr.get("title_patterns") or []:
        try:
            titles.append(re.compile(str(pat), re.IGNORECASE))
        except re.error as exc:
            errors.append(f"irreversible.title_patterns: {pat!r}: {exc}")
    overrides = []
    for i, o in enumerate(raw.get("overrides") or []):
        if not isinstance(o, dict) or not o.get("repo") or o.get("level") not in LEVELS or not o.get("reason"):
            errors.append(f"overrides[{i}]: needs repo, level (one of {LEVELS}) and a reason")
            continue
        overrides.append({"repo": str(o["repo"]), "class": str(o.get("class") or "*"),
                          "level": str(o["level"]), "reason": str(o["reason"])})
    for key in ("window_prs", "min_days_at_level"):
        if not isinstance(raw.get(key, 1), int) or raw.get(key, 1) < 0:
            errors.append(f"{key}: must be a non-negative integer")
    if errors:
        raise ValueError(f"{path}: " + "; ".join(errors))
    return Config(
        default=default, floor=floor, ceiling=ceiling,
        window_prs=int(raw.get("window_prs", 20)), min_days_at_level=int(raw.get("min_days_at_level", 30)),
        thresholds={k: float(v) for k, v in th.items()},
        irreversible_classes=frozenset(str(c) for c in irr.get("classes") or []),
        irreversible_paths=tuple(str(p) for p in irr.get("paths") or []),
        irreversible_titles=tuple(titles), overrides=tuple(overrides))


def scope_key(repo: str, finding_class: str) -> str:
    return f"{repo}/{finding_class or UNCLASSIFIED}"


def _override(cfg: Config, repo: str, finding_class: str) -> dict[str, str] | None:
    cls = finding_class or UNCLASSIFIED
    exact = [o for o in cfg.overrides if o["repo"] == repo and o["class"] == cls]
    return (exact or [o for o in cfg.overrides if o["repo"] == repo and o["class"] == "*"] or [None])[0]


# ── the nightly step ─────────────────────────────────────────────────────────

def _pct(x: float) -> str:
    return f"{x * 100:.1f}%".replace(".0%", "%")


def step(level: str, since: datetime, metrics: dict[str, Any] | None, cfg: Config,
         now: datetime) -> tuple[str, str]:
    """One night's move for a scope: (new level, why)."""
    m = metrics or {}
    breaches = [f"{k.replace('_rate', '').replace('_', ' ')} {_pct(m[k])} > {_pct(t)}"
                for k, t in cfg.thresholds.items() if isinstance(m.get(k), (int, float)) and m[k] > t]
    if breaches:
        if _rank(level) <= _rank(cfg.floor):
            return level, f"held at {level} (the floor): " + "; ".join(breaches)
        return LEVELS[_rank(level) - 1], "demoted: " + "; ".join(breaches)
    if _rank(level) >= _rank(cfg.ceiling):
        return level, f"at the ceiling ({cfg.ceiling})"
    missing = []
    prs = int(m.get("prs") or 0)
    if prs < cfg.window_prs:
        missing.append(f"{prs} of {cfg.window_prs} mature agent PRs")
    if m.get("unproven"):
        missing.append(f"{m['unproven']} pack(s) resolved without evidence")
    days = (now - since).days
    if days < cfg.min_days_at_level:
        missing.append(f"{days} of {cfg.min_days_at_level} days at {level}")
    if missing:
        return level, "not yet: " + "; ".join(missing)
    return LEVELS[_rank(level) + 1], f"promoted: {prs} PRs within every threshold, {days} days at {level}"


def _parse(ts: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def load_state(output_dir: str | Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(output_dir) / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"scopes": {}}
    return data if isinstance(data, dict) and isinstance(data.get("scopes"), dict) else {"scopes": {}}


def refresh(output_dir: str | Path, *, trust_report: dict[str, Any] | None = None, cfg: Config | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    """Move every scope one step, write ``autonomy.json`` and append to the history."""
    from auditor.trust_metrics import load_report

    out = Path(output_dir)
    cfg = cfg or load_config()
    now = now or datetime.now(UTC)
    report = trust_report if trust_report is not None else (load_report(out) or {})
    by_scope = report.get("by_scope") or {}
    prev = load_state(out)["scopes"]
    scopes: dict[str, Any] = {}
    changes = []
    for key in sorted(set(by_scope) | set(prev)):
        repo, _, cls = key.partition("/")
        old = prev.get(key) or {}
        level = old.get("level") if old.get("level") in LEVELS else cfg.default
        since = _parse(old.get("since")) or now
        pin = _override(cfg, repo, cls)
        if pin:
            new, why = pin["level"], f"pinned: {pin['reason']}"
        else:
            new, why = step(level, since, by_scope.get(key), cfg, now)
            if _rank(new) > _rank(cfg.ceiling):  # the ceiling was lowered since
                new, why = cfg.ceiling, f"lowered to the ceiling ({cfg.ceiling})"
        if new != level:
            changes.append({"scope": key, "from": level, "to": new, "reason": why})
            since = now
        scopes[key] = {"level": new, "since": since.isoformat(), "reason": why, "metrics": by_scope.get(key)}
    state = {"generated_at": now.isoformat(), "default": cfg.default, "ceiling": cfg.ceiling,
             "window_prs": cfg.window_prs, "scopes": scopes}
    atomic_write_text(out / STATE_NAME, json.dumps(state, indent=1))
    with (out / HISTORY_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now.isoformat(), "changes": changes,
                             "levels": dict(Counter(s["level"] for s in scopes.values()))}) + "\n")
    return state


def refresh_quietly(output_dir: str | Path) -> dict[str, Any]:
    """Pipeline tail: never fails the run. A bad config keeps yesterday's levels and says why."""
    try:
        state = refresh(output_dir)
    except Exception as exc:  # noqa: BLE001 — the levels stay as they were; the log says why
        logger.warning("autonomy refresh failed: %s", exc)
        return {"error": str(exc)[:300]}
    return {"scopes": len(state["scopes"]),
            "levels": dict(Counter(s["level"] for s in state["scopes"].values()))}


# ── asking ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    level: str
    reason: str
    scope: str
    irreversible: bool = False

    @property
    def may_open_pr(self) -> bool:
        return _rank(self.level) >= _rank("draft_pr")

    @property
    def draft(self) -> bool:
        return self.level == "draft_pr"


def irreversible_reason(cfg: Config, finding_class: str, paths: list[str] | tuple[str, ...], title: str) -> str:
    """Why a change counts as irreversible (auth, secrets, migrations, data deletion), or ""."""
    if finding_class in cfg.irreversible_classes:
        return f"class {finding_class}"
    for p in paths:
        hit = next((g for g in cfg.irreversible_paths if fnmatch.fnmatch(p.lower(), g.lower())), None)
        if hit:
            return f"{p} matches {hit}"
    for pat in cfg.irreversible_titles:
        if pat.search(title or ""):
            return f"title matches {pat.pattern}"
    return ""


def decide(repo: str, finding_class: str = "", *, paths: list[str] | tuple[str, ...] = (), title: str = "",
           output_dir: str | Path | None = None, cfg: Config | None = None) -> Decision:
    """The level the conductor may act at for this change, and why."""
    cfg = cfg or load_config()
    key = scope_key(repo, finding_class)
    pin = _override(cfg, repo, finding_class)
    if pin:
        level, why = pin["level"], f"pinned: {pin['reason']}"
    else:
        cur = load_state(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")["scopes"].get(key) or {}
        level = cur.get("level") if cur.get("level") in LEVELS else cfg.default
        why = str(cur.get("reason") or f"{cfg.default} (default: no measured history yet)")
        if _rank(level) > _rank(cfg.ceiling):
            level, why = cfg.ceiling, f"capped at the ceiling ({cfg.ceiling})"
    irr = irreversible_reason(cfg, finding_class, paths, title)
    if irr and _rank(level) > _rank("draft_pr"):
        return Decision("draft_pr", f"irreversible ({irr}): never above draft_pr", key, True)
    return Decision(level, why, key, bool(irr))

