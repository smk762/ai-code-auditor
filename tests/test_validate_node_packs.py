"""Regression tests for auditor/validate_node.py audit packs."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from auditor import validate_node

needs_npm = pytest.mark.skipif(shutil.which("npm") is None or shutil.which("node") is None,
                               reason="node/npm not installed")


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@needs_npm
def test_file_names_are_never_node_options(tmp_path: Path) -> None:  # 3608ace407
    _write(tmp_path, {"--x.js": "const a = 1;\n"})
    r = validate_node._syntax_check(tmp_path, ["--x.js"], 30)
    assert r.passed, r.raw_output
    _write(tmp_path, {"--y.js": "function ( {\n"})
    r = validate_node._syntax_check(tmp_path, ["--y.js"], 30)
    assert not r.passed and "SyntaxError" in r.raw_output


# The "test" script prints what a shell prints for a missing binary, but only once the check is
# running and only when src/ holds a line with MISSING: the failure comes from the code under test.
_SPAWN = """
import { readdirSync, readFileSync } from "node:fs";
for (const f of readdirSync("src")) {
  if (readFileSync("src/" + f, "utf8").includes("MISSING")) {
    console.log("sh: 1: helper-tool: not found");
    process.exit(127);
  }
}
"""


def _project(root: Path, src: str) -> None:
    pkg = {"name": "web", "version": "1.0.0", "private": True, "scripts": {"test": "node spawn.mjs"}}
    lock = {"name": "web", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
            "packages": {"": {"name": "web", "version": "1.0.0"}}}
    _write(root, {"package.json": json.dumps(pkg), "package-lock.json": json.dumps(lock),
                  "spawn.mjs": _SPAWN, "src/a.ts": src})


@needs_npm
def test_missing_command_from_inside_a_check_is_a_failure(tmp_path: Path) -> None:  # dec6428e3a
    base, patched = tmp_path / "base", tmp_path / "patched"
    _project(base / "web", "export const a = 1;\n")
    _project(patched / "web", "export const a = 'MISSING';\n")
    out = validate_node.validate_node_projects(patched, ["web/src/a.ts"], cache_dir=tmp_path / "node",
                                               base_tree=lambda: base, timeout_s=60)
    assert out.ran, (out.notes, out.unvalidated)
    (_, res), = out.ran
    assert not res.passed, out.notes
    assert "helper-tool" in res.raw_output

    # The same "not found" on the base tree too: an environment gap, not this patch's failure.
    _project(base / "web", "export const a = 'MISSING';\n")
    out = validate_node.validate_node_projects(patched, ["web/src/a.ts"], cache_dir=tmp_path / "node",
                                               base_tree=lambda: base, timeout_s=60)
    (_, res), = out.ran
    assert res.passed and any("fails on the base branch too" in n for n in out.notes), out.notes


def test_env_gap_only_when_the_scripts_own_command_is_missing(tmp_path: Path) -> None:  # dec6428e3a
    _write(tmp_path, {"package.json": json.dumps({"scripts": {"typecheck": "NODE_ENV=x tsc --noEmit",
                                                              "test": "vitest run"}})})
    cause = validate_node._env_gap_is_the_cause
    assert cause(tmp_path, "typecheck", "sh: 1: tsc: not found\n")
    assert cause(tmp_path, "test", "bash: line 1: vitest: command not found\n")
    assert cause(tmp_path, "test", "env: node: No such file or directory\n")
    assert not cause(tmp_path, "test", " FAIL src/a.test.ts\nsh: 1: helper-tool: not found\n")
    assert not cause(tmp_path, "test", "Error: Cannot find module 'x'\nsh: 1: tsc: not found\n")


def test_symlinked_manifest_is_never_followed(tmp_path: Path, monkeypatch) -> None:  # e2c5a85d21
    secret = tmp_path / "host-secret"
    secret.write_text("TOKEN=hunter2\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(validate_node.subprocess, "run", lambda *a, **k: calls.append(a))
    for name in ("package-lock.json", "package.json"):
        root = tmp_path / f"proj-{name}"
        root.mkdir()
        _write(root, {"package.json": "{}", "package-lock.json": "{}"})
        (root / name).unlink()
        (root / name).symlink_to(secret)
        modules, err = validate_node.ensure_node_modules(root, tmp_path / "cache")
        assert modules is None and "symlink" in err
    assert not calls
    assert not any("hunter2" in p.read_text(errors="ignore")
                   for p in (tmp_path / "cache").rglob("*") if p.is_file())


def test_missing_package_json_is_an_error_not_a_crash(tmp_path: Path) -> None:  # 5d028742da
    _write(tmp_path, {"package-lock.json": "{}"})
    modules, err = validate_node.ensure_node_modules(tmp_path, tmp_path / "cache")
    assert modules is None and "package.json" in err


def test_stale_or_dangling_node_modules_link_is_replaced(tmp_path: Path) -> None:  # 08db2afecb
    root = tmp_path / "proj"
    root.mkdir()
    old, new = tmp_path / "old_modules", tmp_path / "new_modules"
    new.mkdir()
    (root / "node_modules").symlink_to(old)  # dangling: an evicted cache entry
    validate_node._link_modules(root, new)
    assert (root / "node_modules").resolve() == new.resolve()
    old.mkdir()
    (root / "node_modules").unlink()
    (root / "node_modules").symlink_to(old)  # live, but another lockfile's deps
    validate_node._link_modules(root, new)
    assert (root / "node_modules").resolve() == new.resolve()
    validate_node._link_modules(root, new)  # already right: kept
    assert (root / "node_modules").resolve() == new.resolve()


def test_drain_after_kill_is_bounded(tmp_path: Path, monkeypatch) -> None:  # 9f8ac222cf
    class Proc:
        pid = 0
        returncode = -9
        stdout = None

        def communicate(self, timeout=None):
            if timeout is None:
                raise AssertionError("unbounded communicate() would hang on an escaped child")
            raise validate_node.subprocess.TimeoutExpired("npm", timeout)

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(validate_node.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(validate_node.os, "killpg", lambda *a: None)
    r = validate_node._run(["npm", "test"], tmp_path, 1)
    assert not r.passed and "timed out" in r.errors[0]


def test_error_ids_do_not_depend_on_hash_seed(tmp_path: Path) -> None:  # b8b8e513ac
    import os
    import subprocess
    import sys

    (tmp_path / "repo").mkdir()
    code = ("import json, os; from pathlib import Path; from auditor.validate_node import error_ids; "
            "out = str(Path('repo').resolve()) + os.sep + 'src/a.ts: drift found'; "
            "print(json.dumps(sorted(error_ids('wire:check', out, Path('repo')))))")
    env = {**os.environ, "PYTHONPATH": str(Path(validate_node.__file__).resolve().parents[1])}
    for seed in range(8):
        out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                             env={**env, "PYTHONHASHSEED": str(seed)}, check=True).stdout
        assert json.loads(out) == ["src/a.ts: drift found"], (seed, out)


@pytest.mark.parametrize("pkg", ["[]", '"x"', "null", '{"scripts": "lint"}', '{"scripts": ["lint", "test"]}',
                                 '{"scripts": {"test": null, "lint": 1}}'])
def test_odd_package_json_shapes_mean_no_checks(tmp_path: Path, pkg: str) -> None:  # f4db7f3399
    _write(tmp_path, {"package.json": pkg})
    assert validate_node.project_checks(tmp_path) == []
    assert validate_node._env_gap_is_the_cause(tmp_path, "test", "sh: 1: vitest: not found") is False
