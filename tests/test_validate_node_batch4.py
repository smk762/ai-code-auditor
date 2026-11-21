"""Regression tests for auditor/validate_node.py audit packs (batch 4)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from auditor import validate_node


def _proj(root: Path, pkg: dict, lock: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (root / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    return root


def _lock(**packages) -> dict:
    return {"name": "web", "lockfileVersion": 3, "packages": {"": {"name": "web"}, **packages}}


@pytest.fixture
def npm_calls(monkeypatch) -> list:
    calls: list = []

    def fake_npm(cmd, *a, cwd=None, **k):
        if cmd[0] != "npm":  # `node --version`, for the cache key
            return subprocess.CompletedProcess(cmd, 0, "v0-test", "")
        calls.append(cmd)
        (Path(cwd) / "node_modules").mkdir(exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(validate_node.subprocess, "run", fake_npm)
    monkeypatch.delenv("AI_AUDIT_NPM_REGISTRIES", raising=False)
    monkeypatch.delenv("npm_config_registry", raising=False)
    return calls


@pytest.mark.parametrize("pkg,lock", [
    ({"dependencies": {"x": "1.0.0"}},
     _lock(**{"node_modules/x": {"version": "1.0.0", "resolved": "http://169.254.169.254/latest/x.tgz"}})),
    ({"dependencies": {"x": "1.0.0"}},
     _lock(**{"node_modules/x": {"version": "1.0.0", "resolved": "git+ssh://git@internal/x.git#abc"}})),
    ({"dependencies": {"x": "1.0.0"}},
     _lock(**{"node_modules/x": {"resolved": "../../../home/smk/.ssh", "link": True}})),
    ({"dependencies": {"x": "1.0.0"}}, _lock(**{"../../etc": {"version": "1.0.0"}})),
    ({"dependencies": {"x": "file:/home/smk/.ssh"}}, _lock()),
    ({"dependencies": {"x": "evil/repo"}}, _lock()),
    ({"workspaces": ["../../.."]}, _lock()),
    ({}, {"lockfileVersion": 1, "dependencies": {"x": {"version": "file:../../secret"}}}),
])
def test_untrusted_lockfile_never_reaches_npm(tmp_path: Path, npm_calls: list, pkg, lock) -> None:  # 2afb07b861
    root = _proj(tmp_path / "web", pkg, lock)
    modules, err = validate_node.ensure_node_modules(root, tmp_path / "cache")
    assert modules is None and "not installed" in err
    assert npm_calls == []


def test_registry_lockfile_still_installs(tmp_path: Path, npm_calls: list, monkeypatch) -> None:  # 2afb07b861
    lock = _lock(**{"node_modules/x": {"version": "1.0.0", "resolved": "https://registry.npmjs.org/x/-/x-1.0.0.tgz"},
                    "node_modules/@s/y": {"version": "npm:z@2.0.0",
                                          "resolved": "https://npm.example.com/z/-/z-2.0.0.tgz"}})
    root = _proj(tmp_path / "web", {"dependencies": {"x": "^1.0.0", "@s/y": "npm:z@^2"}}, lock)
    modules, err = validate_node.ensure_node_modules(root, tmp_path / "cache")
    assert modules is None and "npm.example.com" in err  # not allow-listed yet
    monkeypatch.setenv("AI_AUDIT_NPM_REGISTRIES", "https://npm.example.com/")
    modules, err = validate_node.ensure_node_modules(root, tmp_path / "cache")
    assert modules is not None and err == "" and len(npm_calls) == 1


def test_leftover_entry_that_rmtree_could_not_clear_is_rebuilt(tmp_path: Path, npm_calls: list,
                                                               monkeypatch) -> None:  # 99a7bb26bb
    root = _proj(tmp_path / "web", {}, _lock())
    fake_npm = validate_node.subprocess.run

    def killed_install(cmd, *a, cwd=None, **k):
        if cmd[0] != "npm":
            return fake_npm(cmd, *a, cwd=cwd, **k)
        (Path(cwd) / "node_modules" / "half").mkdir(parents=True)
        raise subprocess.CalledProcessError(1, cmd, "", "killed")

    monkeypatch.setattr(validate_node.shutil, "rmtree", lambda *a, **k: None)  # e.g. a permission problem
    monkeypatch.setattr(validate_node.subprocess, "run", killed_install)
    assert validate_node.ensure_node_modules(root, tmp_path / "cache")[0] is None
    monkeypatch.setattr(validate_node.subprocess, "run", fake_npm)
    modules, err = validate_node.ensure_node_modules(root, tmp_path / "cache")
    assert modules is not None and err == "", err
