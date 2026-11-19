from __future__ import annotations

import subprocess

import pytest

from auditor import static_tools


@pytest.mark.parametrize("level,expected", [
    ("CRITICAL", "CRITICAL"), ("HIGH", "HIGH"), ("MEDIUM", "MEDIUM"), ("LOW", "LOW"),
    ("ERROR", "HIGH"), ("WARNING", "MEDIUM"), ("INFO", "LOW"), ("SOMETHING-NEW", "MEDIUM"),
])
def test_semgrep_severity_levels_are_not_downgraded(level: str, expected: str) -> None:
    assert static_tools._semgrep_to_severity(level) == expected


@pytest.mark.parametrize("runner", ["_run_bandit", "_run_semgrep"])
def test_static_tools_run_with_a_timeout_and_skip_when_it_expires(
    runner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)

    monkeypatch.setattr(static_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(static_tools.subprocess, "run", fake_run)
    assert getattr(static_tools, runner)("r", ["/x"]) == []
    assert seen.get("timeout") and seen["timeout"] > 0


def _fake_tool(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    import json

    monkeypatch.setattr(static_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(static_tools.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, json.dumps(payload), ""))


def test_bandit_ids_stay_distinct_when_several_tests_hit_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from auditor.finding_class import static_rule_id

    hit = {"filename": "a.py", "line_number": 3, "issue_severity": "HIGH"}
    _fake_tool(monkeypatch, {"results": [{**hit, "test_id": "B602"}, {**hit, "test_id": "B607"},
                                         {**hit, "test_id": "B607"}]})
    ids = [f.id for f in static_tools._run_bandit("r", ["a.py"])]
    assert ids == ["bandit:B602:a.py:3", "bandit:B607:a.py:3", "bandit:B607:a.py:3:2"]
    assert static_rule_id(ids[0], "bandit") == "B602"


def test_semgrep_ids_stay_distinct_when_several_rules_hit_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = {"path": "a.py", "start": {"line": 9}, "extra": {"severity": "ERROR"}}
    _fake_tool(monkeypatch, {"results": [{**hit, "check_id": "py.rule-a"}, {**hit, "check_id": "py.rule-b"}]})
    ids = [f.id for f in static_tools._run_semgrep("r", ["a.py"])]
    assert ids == ["semgrep:py.rule-a:a.py:9", "semgrep:py.rule-b:a.py:9"]
