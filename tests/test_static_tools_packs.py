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
