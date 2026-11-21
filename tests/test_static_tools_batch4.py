"""static_tools: a failed semgrep run is reported, never passed off silently as a clean scan."""
from __future__ import annotations

import json
import logging
import subprocess

import pytest

from auditor import static_tools


def _semgrep(monkeypatch: pytest.MonkeyPatch, code: int, stdout: str, stderr: str = "") -> None:
    monkeypatch.setattr(static_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(static_tools.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, code, stdout, stderr))


def test_a_failed_semgrep_run_is_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _semgrep(monkeypatch, 2, "", "Failed to download configuration from semgrep.dev")
    with caplog.at_level(logging.WARNING, logger="auditor.static_tools"):
        assert static_tools._run_semgrep("r", ["/x"]) == []
    assert any("semgrep failed (exit 2)" in r.getMessage() and "semgrep.dev" in r.getMessage()
               for r in caplog.records)


def test_unparseable_semgrep_output_is_logged(monkeypatch: pytest.MonkeyPatch,
                                              caplog: pytest.LogCaptureFixture) -> None:
    _semgrep(monkeypatch, 0, "<html>rate limited</html>")
    with caplog.at_level(logging.WARNING, logger="auditor.static_tools"):
        assert static_tools._run_semgrep("r", ["/x"]) == []
    assert any("not JSON" in r.getMessage() for r in caplog.records)


def test_semgrep_errors_in_the_payload_are_logged_and_results_kept(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    payload = {"results": [{"check_id": "rule", "path": "a.py", "start": {"line": 2}, "extra": {}}],
               "errors": [{"type": "Syntax error", "message": "could not parse b.py"}]}
    _semgrep(monkeypatch, 1, json.dumps(payload))
    with caplog.at_level(logging.WARNING, logger="auditor.static_tools"):
        found = static_tools._run_semgrep("r", ["/x"])
    assert [f.title for f in found] == ["rule"]
    assert any("1 error(s)" in r.getMessage() and "b.py" in r.getMessage() for r in caplog.records)
