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
