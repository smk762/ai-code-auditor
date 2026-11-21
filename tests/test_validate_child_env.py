"""Code under validation never sees the host's secrets or live service URLs.

validate_patch runs a patch's tests, installs and checks inside the API container, whose env holds
ANTHROPIC_API_KEY, GITHUB_TOKEN, the API token, DATABASE_URL and the RAG ingest URL and secret.
A test that found the RAG URL there made a real send that blocked for the whole run timeout."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from auditor import evidence, validator
from auditor.proc import child_env

SECRETS = {
    "ANTHROPIC_API_KEY": "sk-test", "GITHUB_TOKEN": "ghp_test", "AI_AUDIT_API_TOKEN": "tok",
    "AI_AUDIT_RAG_INGEST_URL": "http://192.0.2.1:9050/ingest", "INGEST_SHARED_SECRET": "s",
    "DATABASE_URL": "postgresql://u:p@db/x",
}
PROBE = "import os, sys; sys.exit(sorted(k for k in {keys!r} if k in os.environ) and 3 or 0)".format(keys=sorted(SECRETS))
TEST = ("import os\n\n\ndef test_no_host_secrets():\n"
        f"    assert not [k for k in {sorted(SECRETS)!r} if k in os.environ]\n")


@pytest.fixture
def host_secrets(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")


def test_child_env_keeps_only_the_allowlist(host_secrets) -> None:
    env = child_env(PYTHONPATH="/x")
    assert not set(SECRETS) & set(env)
    assert env["PYTHONPATH"] == "/x" and env["LC_ALL"] == "C.UTF-8" and "PATH" in env


def test_exec_runs_tools_without_host_secrets(host_secrets, tmp_path: Path) -> None:
    res = validator._exec([sys.executable, "-c", PROBE], cwd=tmp_path, tool="lint_only", timeout_s=60)
    assert res.passed, res.raw_output


def test_project_tests_run_without_host_secrets(host_secrets, tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_env.py").write_text(TEST)
    outcome, res = validator._run_project_tests(tmp_path, Path(sys.executable), timeout_s=120)
    assert outcome == "ran" and res.passed, res.raw_output


def test_repro_runs_without_host_secrets(host_secrets, tmp_path: Path) -> None:
    (tmp_path / "test_env.py").write_text(TEST)
    outcomes, out = evidence._pytest(tmp_path, Path(sys.executable), ["test_env.py"], timeout_s=120)
    assert outcomes == {"test_env.py::test_no_host_secrets": "passed"}, out


def test_node_checks_run_without_host_secrets(host_secrets) -> None:
    from auditor.validate_node import _env

    env = _env(Path("/tmp/npm-cache"))
    assert not set(SECRETS) & set(env) and env["npm_config_cache"] == "/tmp/npm-cache" and env["CI"] == "1"
