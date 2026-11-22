"""#136: the test-env report must describe the interpreter validate_patch runs on."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auditor import test_env_readiness as ter


def test_validator_python_comes_from_the_dockerfile_or_the_env(monkeypatch) -> None:
    monkeypatch.delenv("AI_AUDIT_VALIDATOR_PYTHON", raising=False)
    text = (ter.REPO_ROOT / "Dockerfile").read_text()
    want = text.split("ARG PYTHON_VERSION=", 1)[1].split()[0]
    assert ter.validator_python() == want and "FROM python:${PYTHON_VERSION}" in text
    assert "ENV AI_AUDIT_VALIDATOR_PYTHON=${PYTHON_VERSION}" in text  # the image knows it too
    monkeypatch.setenv("AI_AUDIT_VALIDATOR_PYTHON", "3.12.4")
    assert ter.validator_python() == "3.12"


def test_a_report_from_another_interpreter_is_served_as_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_VALIDATOR_PYTHON", "3.11")
    (tmp_path / ter.REPORT_NAME).write_text(json.dumps({"python": "3.13.5", "repos": {}}))
    assert ter.load_report(tmp_path)["stale_interpreter"] == "built on Python 3.13, but validate_patch runs on 3.11"
    (tmp_path / ter.REPORT_NAME).write_text(json.dumps({"python": "3.11.9", "repos": {}}))
    assert ter.load_report(tmp_path)["stale_interpreter"] == ""


def test_a_mismatched_interpreter_refuses_without_docker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_VALIDATOR_PYTHON", "2.7")
    monkeypatch.setattr(ter.shutil, "which", lambda _x: None)
    monkeypatch.setattr(ter, "load_ecosystem_config", None, raising=False)
    with pytest.raises(ter.InterpreterMismatch, match="runs on 2.7"):
        ter.run(tmp_path)
    assert not (tmp_path / ter.REPORT_NAME).exists()      # no report from the wrong interpreter
    assert "runs on 2.7" in ter.run_quietly(tmp_path)["skipped"]
    monkeypatch.setattr(ter.shutil, "which", lambda _x: "/usr/bin/docker")
    monkeypatch.setenv("AI_AUDIT_TEST_ENV_IN_IMAGE", "0")
    with pytest.raises(ter.InterpreterMismatch):
        ter.run(tmp_path)


def test_a_mismatched_interpreter_runs_the_check_in_the_api_image(monkeypatch) -> None:
    out = ter.REPO_ROOT / "reports"
    monkeypatch.setenv("AI_AUDIT_VALIDATOR_PYTHON", "2.7")
    monkeypatch.setattr(ter.shutil, "which", lambda _x: "/usr/bin/docker")
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["cwd"]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ter.subprocess, "run", fake_run)
    monkeypatch.setattr(ter, "load_report", lambda _o: {"python": "2.7.18", "repos": {}, "stale_interpreter": ""})
    rep = ter.run(out, repos=["argus-echo"], workers=2)
    [(cmd, cwd)] = calls
    assert cmd[:7] == ["docker", "compose", "run", "--rm", "--no-deps", "-T", "audit-api"]
    assert cmd[7:] == ["ai-audit", "test-env", "--workers", "2", "--repo", "argus-echo"] and cwd == ter.REPO_ROOT
    assert rep["python"] == "2.7.18"
    # the image failing, or writing a report on another interpreter, is a refusal, not a result
    monkeypatch.setattr(ter, "load_report", lambda _o: {"python": "3.13.5", "repos": {}})
    with pytest.raises(ter.InterpreterMismatch, match="API image failed"):
        ter.run(out)
    with pytest.raises(ter.InterpreterMismatch, match="writes its report to"):
        ter.run(Path("/tmp/elsewhere"))


def test_a_partial_run_never_keeps_another_interpreters_statuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_VALIDATOR_PYTHON", ter.this_python())
    (tmp_path / ter.REPORT_NAME).write_text(json.dumps({"python": "1.0", "repos": {"old": {"projects": []}}}))
    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: SimpleNamespace(
        repos=[SimpleNamespace(name="r", enabled=True)], output_dir=str(tmp_path)))
    monkeypatch.setattr(ter, "check_repo", lambda *a, **k: {"projects": []})
    monkeypatch.setattr("auditor.repo_resolver.resolve_repo_path", lambda r: tmp_path)
    monkeypatch.setenv("AI_AUDIT_VALIDATOR_PYTHON", ter.this_python())
    rep = ter.run(tmp_path, repos=["r"])
    assert list(rep["repos"]) == ["r"]
