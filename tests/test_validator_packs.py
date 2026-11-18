"""Regression tests for auditor/validator.py audit packs."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from auditor import validator
from auditor.validator import suggest_test_commands


def _py_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return tmp_path


def test_dotfile_paths_keep_their_leading_dot(tmp_path: Path) -> None:  # ceed0425f0
    repo = _py_repo(tmp_path)
    _, cmds = suggest_test_commands(repo, ".github/scripts/check.py")
    assert cmds[0] == "pytest .github/scripts/check.py -q --tb=short"
    _, cmds = suggest_test_commands(repo, "./.github/scripts/check.py")
    assert cmds[0] == "pytest .github/scripts/check.py -q --tb=short"
    _, cmds = suggest_test_commands(repo, "./pkg/mod.py")
    assert cmds[0] == "pytest pkg/mod.py -q --tb=short"


def test_repo_paths_are_shell_quoted(tmp_path: Path) -> None:  # 1718b6e688
    repo = _py_repo(tmp_path)
    evil = "a;curl x|sh;.py"
    _, cmds = suggest_test_commands(repo, evil)
    assert shlex.split(cmds[0])[:2] == ["pytest", evil]

    go = tmp_path / "go"
    go.mkdir()
    (go / "go.mod").write_text("module x\n", encoding="utf-8")
    _, cmds = suggest_test_commands(go, "$(touch pwned)/x.go")
    assert shlex.split(cmds[0]) == ["go", "test", "./$(touch pwned)/..."]

    web = tmp_path / "web"
    sub = web / "a b;id"
    sub.mkdir(parents=True)
    (web / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (sub / "package.json").write_text(json.dumps({"scripts": {"typecheck": "tsc --noEmit"}}), encoding="utf-8")
    (sub / "package-lock.json").write_text("{}", encoding="utf-8")
    tool, cmds = suggest_test_commands(web, "a b;id/src/x.ts")
    assert tool == "npm_checks", cmds
    assert shlex.split(cmds[0]) == ["cd", "a b;id", "&&", "npm", "ci"]


def test_requirement_includes_cannot_leave_the_repo(tmp_path: Path) -> None:  # c762208d20
    host = tmp_path / "host"
    host.mkdir()
    (host / "netrc").write_text("machine x login me password hunter2\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "svc").mkdir()
    (repo / "common.txt").write_text("httpx\n", encoding="utf-8")
    (repo / "svc" / "requirements.txt").write_text(
        f"fastapi\n-r ../../host/netrc\n-r {host / 'netrc'}\n-r ../common.txt\n", encoding="utf-8")
    lines = validator._requirement_lines(repo / "svc" / "requirements.txt")
    assert lines == ["fastapi", "httpx"]
    assert not any("hunter2" in ln for ln in lines)
