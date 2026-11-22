"""Trust-gated autonomy (#61): promotion, demotion, the irreversible-action denylist."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from auditor import autonomy as au
from auditor import trust_metrics as tm

NOW = datetime(2026, 12, 20, tzinfo=UTC)
CLEAN = {"prs": 20, "packs": 30, "reverted": 0, "revert_rate": 0.0, "observed": 30, "regressed": 0,
         "recurrence_rate": 0.0, "no_tests": 0, "false_resolve_rate": 0.0, "unproven": 0}


def cfg(**kw) -> au.Config:
    base = {"irreversible": {"classes": ["secret_exposure", "fail_open"],
                             "paths": ["*migrations/*", "*auth*", "*.sql"],
                             "title_patterns": [r"\b(delete|drop|truncate)\b"]}}
    return _load(base | kw)


def _load(raw: dict) -> au.Config:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "autonomy.yaml"
        path.write_text(yaml.safe_dump(raw))
        return au.load_config(path)


# ── the nightly step ─────────────────────────────────────────────────────────

def test_promotion_needs_a_full_clean_window_evidence_and_a_month() -> None:
    c = cfg()
    month_ago = NOW - timedelta(days=31)
    assert au.step("draft_pr", month_ago, CLEAN, c, NOW)[0] == "ready_pr"
    level, why = au.step("draft_pr", month_ago, {**CLEAN, "prs": 19}, c, NOW)
    assert level == "draft_pr" and "19 of 20 mature agent PRs" in why
    level, why = au.step("draft_pr", month_ago, {**CLEAN, "unproven": 2}, c, NOW)
    assert level == "draft_pr" and "without evidence" in why
    level, why = au.step("draft_pr", NOW - timedelta(days=10), CLEAN, c, NOW)
    assert level == "draft_pr" and "10 of 30 days" in why


def test_nothing_is_promoted_past_the_ceiling() -> None:
    level, why = au.step("ready_pr", NOW - timedelta(days=90), CLEAN, cfg(), NOW)
    assert level == "ready_pr" and "ceiling" in why


@pytest.mark.parametrize("breach", [{"revert_rate": 0.05, "reverted": 1}, {"recurrence_rate": 0.06},
                                    {"false_resolve_rate": 0.1}])
def test_any_breach_demotes_one_level(breach: dict) -> None:
    level, why = au.step("ready_pr", NOW - timedelta(days=90), {**CLEAN, **breach}, cfg(), NOW)
    assert level == "draft_pr" and why.startswith("demoted")


def test_a_breach_never_demotes_below_the_floor_unless_configured() -> None:
    bad = {**CLEAN, "recurrence_rate": 0.25}
    level, why = au.step("draft_pr", NOW, bad, cfg(), NOW)
    assert level == "draft_pr" and "the floor" in why and "recurrence 25% > 5%" in why
    assert au.step("draft_pr", NOW, bad, cfg(floor="report"), NOW)[0] == "report"


def test_no_metrics_holds_the_level() -> None:
    level, why = au.step("draft_pr", NOW, None, cfg(), NOW)
    assert level == "draft_pr" and "0 of 20" in why


# ── refresh: state and history ───────────────────────────────────────────────

def test_refresh_moves_each_scope_one_step_and_appends_history(tmp_path: Path) -> None:
    c = cfg()
    report = {"by_scope": {"a/ssrf": CLEAN, "a/unclassified": {**CLEAN, "prs": 3}}}
    first = au.refresh(tmp_path, trust_report=report, cfg=c, now=NOW)
    assert {k: v["level"] for k, v in first["scopes"].items()} == {"a/ssrf": "draft_pr", "a/unclassified": "draft_pr"}
    later = NOW + timedelta(days=31)
    second = au.refresh(tmp_path, trust_report=report, cfg=c, now=later)
    assert second["scopes"]["a/ssrf"]["level"] == "ready_pr"
    assert second["scopes"]["a/ssrf"]["since"] == later.isoformat()
    assert second["scopes"]["a/unclassified"]["level"] == "draft_pr"
    # a scope the metrics no longer list keeps its level
    third = au.refresh(tmp_path, trust_report={"by_scope": {}}, cfg=c, now=later + timedelta(days=1))
    assert third["scopes"]["a/ssrf"]["level"] == "ready_pr"
    lines = [json.loads(x) for x in (tmp_path / au.HISTORY_NAME).read_text().splitlines()]
    assert len(lines) == 3 and lines[1]["changes"] == [
        {"scope": "a/ssrf", "from": "draft_pr", "to": "ready_pr", "reason": second["scopes"]["a/ssrf"]["reason"]}]


def test_lowering_the_ceiling_takes_effect_on_the_next_refresh(tmp_path: Path) -> None:
    (tmp_path / au.STATE_NAME).write_text(json.dumps({"scopes": {"a/ssrf": {"level": "ready_pr", "since": NOW.isoformat()}}}))
    st = au.refresh(tmp_path, trust_report={}, cfg=cfg(ceiling="draft_pr"), now=NOW)
    assert st["scopes"]["a/ssrf"]["level"] == "draft_pr"


def test_an_override_pins_a_scope(tmp_path: Path) -> None:
    c = cfg(overrides=[{"repo": "a", "class": "*", "level": "report", "reason": "release freeze"}])
    st = au.refresh(tmp_path, trust_report={"by_scope": {"a/ssrf": CLEAN}}, cfg=c, now=NOW)
    assert st["scopes"]["a/ssrf"]["level"] == "report"
    d = au.decide("a", "ssrf", output_dir=tmp_path, cfg=c)
    assert not d.may_open_pr and "release freeze" in d.reason


def test_refresh_quietly_keeps_yesterdays_levels_on_a_bad_config(tmp_path: Path, monkeypatch) -> None:
    bad = tmp_path / "autonomy.yaml"
    bad.write_text("ceiling: yolo\n")
    monkeypatch.setenv("AI_AUDIT_AUTONOMY_CONFIG", str(bad))
    assert "error" in au.refresh_quietly(tmp_path)
    assert not (tmp_path / au.STATE_NAME).exists()


# ── decide: what the conductor asks ──────────────────────────────────────────

def test_an_unmeasured_scope_gets_the_default(tmp_path: Path) -> None:
    d = au.decide("a", "", output_dir=tmp_path, cfg=cfg())
    assert (d.level, d.scope, d.draft, d.may_open_pr) == ("draft_pr", "a/unclassified", True, True)


@pytest.mark.parametrize(("cls", "path", "title"), [
    ("secret_exposure", "app/x.py", "t"),
    ("ssrf", "app/migrations/0004_drop.py", "t"),
    ("ssrf", "src/svc/auth/tokens.py", "t"),
    ("ssrf", "schema.SQL", "t"),
    ("ssrf", "app/x.py", "Delete stale rows on startup"),
])
def test_irreversible_changes_never_go_above_draft_pr(tmp_path: Path, cls: str, path: str, title: str) -> None:
    (tmp_path / au.STATE_NAME).write_text(json.dumps({"scopes": {f"a/{cls}": {"level": "ready_pr"}}}))
    c = cfg(ceiling="auto_merge")
    d = au.decide("a", cls, paths=[path], title=title, output_dir=tmp_path, cfg=c)
    assert d.level == "draft_pr" and d.irreversible and d.reason.startswith("irreversible")


def test_a_reversible_change_gets_the_scope_level(tmp_path: Path) -> None:
    (tmp_path / au.STATE_NAME).write_text(json.dumps({"scopes": {"a/ssrf": {"level": "ready_pr", "reason": "promoted"}}}))
    d = au.decide("a", "ssrf", paths=["app/client.py"], title="Pin outbound hosts", output_dir=tmp_path, cfg=cfg())
    assert (d.level, d.draft, d.irreversible) == ("ready_pr", False, False)


@pytest.mark.parametrize("raw", [{"ceiling": "yolo"}, {"default": "ready_pr", "ceiling": "draft_pr"},
                                 {"floor": "ready_pr"}, {"thresholds": {"revert_rate": 2}},
                                 {"thresholds": {"speed": 0.1}}, {"overrides": [{"repo": "a", "level": "report"}]},
                                 {"irreversible": {"title_patterns": ["("]}}])
def test_a_bad_config_is_refused(raw: dict) -> None:
    with pytest.raises(ValueError):
        _load(raw)


def test_the_checked_in_config_loads() -> None:
    c = au.load_config()
    assert (c.floor, c.default, c.ceiling, c.window_prs) == ("draft_pr", "draft_pr", "ready_pr", 20)
    assert {"secret_exposure", "fail_open"} <= c.irreversible_classes


# ── the trust metrics it promotes on ─────────────────────────────────────────

def _pack(pid: str, repo: str, cls: str, note: str) -> tm.PackView:
    return tm.PackView(id=pid, kind="finding", repo=repo, path="x.py", line=1, title=pid, status="resolved",
                       detected_at=None, run_id="r", finding_class=cls,
                       history=[{"status": "resolved", "note": note, "at": "2026-11-01T00:00:00+00:00"}])


def test_scope_metric_counts_each_class_over_its_mature_prs() -> None:
    ok = "validate_patch passed (repo=a, tool=pytest, evidence=repro)"
    packs = [_pack("p1", "a", "ssrf", ok), _pack("p2", "a", "ssrf", "validate_patch passed (repo=a, tool=lint_only)"),
             _pack("p3", "a", "", ok), _pack("p4", "a", "ssrf", "fixed by hand")]
    events = tm.resolution_events(packs)
    rows = [
        {"merged_at": "2026-11-02", "mature": True, "observed": True, "reverts": ["x"], "pack_ids": ["p1", "p3"]},
        {"merged_at": "2026-11-03", "mature": True, "observed": True, "reverts": [], "pack_ids": ["p2", "p4"]},
        {"merged_at": "2026-11-20", "mature": False, "observed": True, "reverts": [], "pack_ids": ["p1"]},
    ]
    out = tm.scope_metric(packs, events, {"p1": "regressed", "p2": "held", "p3": "held"}, rows)
    s = out["a/ssrf"]
    assert (s["prs"], s["packs"], s["reverted"], s["revert_rate"]) == (2, 3, 1, 0.5)
    assert (s["observed"], s["regressed"], s["no_tests"], s["unproven"]) == (2, 1, 1, 2)
    assert out["a/unclassified"]["prs"] == 1 and out["a/unclassified"]["packs"] == 1


def test_scope_window_keeps_the_latest_prs() -> None:
    packs = [_pack(f"p{i}", "a", "ssrf", "validate_patch passed (repo=a, tool=pytest, evidence=tests)") for i in range(25)]
    rows = [{"merged_at": f"2026-11-{i + 1:02d}", "mature": True, "observed": True, "reverts": ["r"] if i < 5 else [],
             "pack_ids": [f"p{i}"]} for i in range(25)]
    s = tm.scope_metric(packs, tm.resolution_events(packs), {}, rows)["a/ssrf"]
    assert s["prs"] == 20 and s["reverted"] == 0 and s["oldest_merged_at"] == "2026-11-06"
