"""Regression tests for auditor/test_env_readiness.py audit packs (batch 5)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from auditor import test_env_readiness as ter


def _mismatch(monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_VALIDATOR_PYTHON", "2.7")
    monkeypatch.setattr(ter.shutil, "which", lambda _x: "/usr/bin/docker")
    monkeypatch.delenv("AI_AUDIT_TEST_ENV_IN_IMAGE", raising=False)


def test_the_mismatch_path_uses_the_configured_output_dir(tmp_path: Path, monkeypatch) -> None:  # b3116e058b
    _mismatch(monkeypatch)
    monkeypatch.delenv("AI_AUDIT_OUTPUT_DIR", raising=False)
    monkeypatch.setattr("auditor.config.load_ecosystem_config",
                        lambda: SimpleNamespace(repos=[], output_dir=str(tmp_path / "configured")))
    seen: list[Path] = []
    monkeypatch.setattr(ter, "_in_api_image", lambda out, *a, **k: seen.append(Path(out)) or {"repos": {}})
    ter.run()
    assert seen == [tmp_path / "configured"]


@pytest.mark.parametrize("body", ["[1, 2]", '"text"', "42", "null"], ids=["list", "string", "number", "null"])
def test_a_report_that_is_not_an_object_is_no_report(tmp_path: Path, body) -> None:  # 71b65eb7db
    (tmp_path / ter.REPORT_NAME).write_text(body)
    assert ter.load_report(tmp_path) is None


def test_the_timeout_reaches_the_check_in_the_api_image(monkeypatch) -> None:  # 15152a6e02
    _mismatch(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(ter.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(ter, "load_report", lambda _o: {"python": "2.7.18", "repos": {}, "stale_interpreter": ""})
    ter.run(ter.REPO_ROOT / "reports", timeout_s=42)
    [cmd] = calls
    assert cmd[cmd.index("--timeout") + 1] == "42"


def test_a_timed_out_image_run_removes_its_container(monkeypatch) -> None:  # df8cda1a67
    _mismatch(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "run"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ter.subprocess, "run", fake_run)
    with pytest.raises(ter.InterpreterMismatch, match="timed out"):
        ter.run(ter.REPO_ROOT / "reports")
    run_cmd, rm_cmd = calls
    name = run_cmd[run_cmd.index("--name") + 1]
    assert run_cmd.index("--name") < run_cmd.index("audit-api")
    assert rm_cmd == ["docker", "rm", "-f", name]


def test_cli_passes_the_timeout(monkeypatch) -> None:
    import cli

    seen: dict = {}
    monkeypatch.setattr(ter, "run", lambda out, **kw: seen.update(kw) or {
        "python": "x", "repos": {}, "summary": {"by_status": {}}})
    monkeypatch.setattr("sys.argv", ["ai-audit", "test-env", "--timeout", "17", "--json"])
    cli.main()
    assert seen["timeout_s"] == 17
    json.dumps(seen)
