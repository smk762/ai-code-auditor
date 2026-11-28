"""validate_patch for TS/JS: each touched npm project's typecheck, lint, *:check and test scripts."""
from __future__ import annotations

import json
import shutil
import sys
import subprocess
from pathlib import Path

import pytest

from auditor import validate_node, validator
from auditor.validate_node import error_ids, node_files, project_checks
from auditor.validator import extract_failure_summary, validate_patch_tree

needs_npm = pytest.mark.skipif(shutil.which("npm") is None or shutil.which("node") is None,
                               reason="node/npm not installed")


class _Repo:
    name = "front"
    path = ""


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


# A dependency-free npm project: `npm ci` needs no network. Its "typecheck" prints a tsc-style
# diagnostic for every line of src/*.ts containing BAD, and its "test" a vitest-style FAIL line
# for every line containing BROKEN, so tests can control which errors exist.
_CHECK = """
import { readdirSync, readFileSync } from "node:fs";
const [mark, kind] = process.argv.slice(2);
let bad = 0;
for (const f of readdirSync("src")) {
  readFileSync("src/" + f, "utf8").split("\\n").forEach((line, i) => {
    if (!line.includes(mark)) return;
    bad++;
    console.log(kind === "tsc" ? `src/${f}(${i + 1},1): error TS2322: ${line.trim()}`
                               : ` FAIL  src/${f} > ${line.trim()}`);
  });
}
process.exit(bad ? 1 : 0);
"""


def _npm_project(root: Path, extra_scripts: dict[str, str] | None = None) -> None:
    scripts = {"typecheck": "node check.mjs BAD tsc", "test": "node check.mjs BROKEN vitest",
               **(extra_scripts or {})}
    pkg = {"name": "web", "version": "1.0.0", "private": True, "scripts": scripts}
    lock = {"name": "web", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
            "packages": {"": {"name": "web", "version": "1.0.0"}}}
    _write(root, {"package.json": json.dumps(pkg), "package-lock.json": json.dumps(lock),
                  "check.mjs": _CHECK, "src/a.ts": "export const a = 1;\n"})


# ── mapping and parsing (no npm needed) ──────────────────────────────────────

def test_node_files_maps_code_anywhere_and_json_only_inside_a_project(tmp_path: Path) -> None:
    _write(tmp_path, {"web/package.json": "{}", "web/src/a.ts": "", "config/x.json": "{}", "web/schema/s.json": "{}"})
    changed = ["web/src/a.ts", "web/src/gone.tsx", "web/schema/s.json", "config/x.json", "app.py",
               "web/node_modules/x/index.js", "README.md"]
    assert node_files(tmp_path, changed) == ["web/schema/s.json", "web/src/a.ts", "web/src/gone.tsx"]


def test_project_checks_run_in_a_fixed_order(tmp_path: Path) -> None:
    _write(tmp_path, {"package.json": json.dumps({"scripts": {
        "test": "vitest run", "wire:check": "node w.mjs --check", "lint": "eslint", "typecheck": "tsc --noEmit",
        "dev": "next dev", "docs:check": "node d.mjs --check", "wire:pull": "node w.mjs --pull"}})})
    checks = project_checks(tmp_path)
    assert [s for s, _ in checks] == ["typecheck", "lint", "docs:check", "wire:check", "test"]
    assert checks[-1][1] == ["run", "-s", "test", "--", "--passWithNoTests"]


def test_npm_default_test_script_is_not_a_check(tmp_path: Path) -> None:
    _write(tmp_path, {"package.json": json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})})
    assert project_checks(tmp_path) == []


def test_error_ids_drop_line_numbers_and_the_tree_path(tmp_path: Path) -> None:
    tsc = "src/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"
    moved = tsc.replace("(3,7)", "(9,1)")
    assert error_ids("typecheck", tsc, tmp_path) == error_ids("typecheck", moved, tmp_path)
    route = "src/app/(shop)/[id]/page.tsx(1,1): error TS2304: Cannot find name 'x'.\n"
    assert list(error_ids("typecheck", route, tmp_path)) == ["src/app/(shop)/[id]/page.tsx: TS2304: Cannot find name 'x'."]

    eslint = (f"\n{tmp_path}/src/lib/api.ts\n"
              "  212:1  error    Unexpected var, use let or const instead          no-var\n"
              "  212:5  warning  'u' is assigned a value but never used  @typescript-eslint/no-unused-vars\n"
              "\n✖ 2 problems (1 error, 1 warning)\n")
    assert dict(error_ids("lint", eslint, tmp_path)) == {
        "src/lib/api.ts: no-var: Unexpected var, use let or const instead": 1}

    vitest = " FAIL  src/lib/api.test.ts > parses ids\n Test Files  1 failed | 4 passed (5)\n"
    assert list(error_ids("test", vitest, tmp_path)) == ["src/lib/api.test.ts > parses ids"]
    # any other script: output lines with timings dropped, so a slower rerun matches
    assert error_ids("wire:check", "drift: src/wire/x.ts (12ms)\n", tmp_path) == \
        error_ids("wire:check", "drift: src/wire/x.ts (340ms)\n", tmp_path)


def test_ts_file_without_a_project_is_never_a_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (Path(sys.executable), ""))
    _write(tmp_path, {"svc/requirements.txt": "", "svc/app/a.py": "X = 1\n",
                      "svc/tests/test_a.py": "def test_a(): pass\n", "scripts/tool.ts": "export {};\n"})
    r = validate_patch_tree(_Repo(), tmp_path, ["svc/app/a.py", "scripts/tool.ts"], cache_dir=tmp_path / "c")
    assert not r.passed and r.tool == "skipped"
    assert "scripts/tool.ts" in r.skipped_reason and "tests passed: svc" in r.raw_output


def test_missing_npm_skips_instead_of_passing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(validate_node.shutil, "which", lambda name: None)
    _npm_project(tmp_path / "web")
    r = validate_patch_tree(_Repo(), tmp_path, ["web/src/a.ts"], cache_dir=tmp_path / "c")
    assert not r.passed and r.tool == "skipped" and "not installed" in r.skipped_reason


def test_failed_install_skips_instead_of_passing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(validate_node, "ensure_node_modules", lambda root, cache: (None, "ETARGET no matching version"))
    _npm_project(tmp_path / "web")
    r = validate_patch_tree(_Repo(), tmp_path, ["web/src/a.ts"], cache_dir=tmp_path / "c")
    assert not r.passed and r.tool == "skipped" and "ETARGET" in r.skipped_reason


def test_failure_summary_for_a_mixed_patch() -> None:
    out = ("== svc\nFAILED tests/test_a.py::test_x - assert 1 == 2\n== web\n-- web npm run typecheck\n"
           "src/a.ts(1,1): error TS2322: nope\nnoise\n")
    summary = extract_failure_summary(out, "pytest+npm_checks")
    assert "src/a.ts(1,1): error TS2322: nope" in summary and "-- web npm run typecheck" in summary
    assert any("test_x" in s for s in summary) and "noise" not in summary


# ── real npm, dependency-free project ────────────────────────────────────────

@needs_npm
def test_clean_ts_patch_passes_with_npm_checks(tmp_path: Path) -> None:
    _npm_project(tmp_path / "tree" / "web")
    r = validate_patch_tree(_Repo(), tmp_path / "tree", ["web/src/a.ts"], cache_dir=tmp_path / "venvs",
                            node_cache_dir=tmp_path / "node")
    assert r.passed and r.tool == "npm_checks", r.raw_output
    assert "npm checks passed: web (typecheck, test)" in r.raw_output
    assert (tmp_path / "tree" / "web" / "node_modules").is_symlink()


@needs_npm
def test_type_error_from_the_patch_fails(tmp_path: Path) -> None:
    _npm_project(tmp_path / "tree" / "web")
    _write(tmp_path / "tree" / "web", {"src/a.ts": "export const a: number = 'BAD';\n"})
    r = validate_patch_tree(_Repo(), tmp_path / "tree", ["web/src/a.ts"], cache_dir=tmp_path / "v",
                            node_cache_dir=tmp_path / "node")
    assert not r.passed and r.tool == "npm_checks"
    assert "error TS2322" in r.raw_output and "-- web npm run typecheck" in r.raw_output


@needs_npm
def test_errors_already_on_the_base_branch_dont_count(tmp_path: Path) -> None:
    base, patched = tmp_path / "base", tmp_path / "patched"
    for t in (base, patched):
        _npm_project(t / "web")
        _write(t / "web", {"src/old.ts": "export const o = 'BAD'; // BROKEN\n"})
    _write(patched / "web", {"src/a.ts": "export const a = 2;\n"})
    r = validate_patch_tree(_Repo(), patched, ["web/src/a.ts"], cache_dir=tmp_path / "v",
                            node_cache_dir=tmp_path / "node", base_tree=lambda: base)
    assert r.passed and r.tool == "npm_checks", r.raw_output
    assert "typecheck fails on the base branch too" in r.raw_output
    assert "test fails on the base branch too" in r.raw_output

    _write(patched / "web", {"src/a.ts": "export const a = 'BAD';\n"})  # a new error on top of the old one
    r = validate_patch_tree(_Repo(), patched, ["web/src/a.ts"], cache_dir=tmp_path / "v",
                            node_cache_dir=tmp_path / "node", base_tree=lambda: base)
    assert not r.passed and "new typecheck errors from this patch" in r.raw_output
    assert "src/a.ts: TS2322" in r.raw_output and "src/old.ts: TS2322" not in r.raw_output.split("\n")[0]


@needs_npm
def test_drift_check_scripts_run(tmp_path: Path) -> None:
    _npm_project(tmp_path / "web", {"wire:check": "node -e \"console.log('drift: src/wire/x.ts'); process.exit(1)\""})
    r = validate_patch_tree(_Repo(), tmp_path, ["web/src/a.ts"], cache_dir=tmp_path / "v",
                            node_cache_dir=tmp_path / "node")
    assert not r.passed and "-- web npm run wire:check" in r.raw_output and "drift: src/wire/x.ts" in r.raw_output


@needs_npm
def test_node_modules_install_once_per_lockfile(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    real_run = validate_node.subprocess.run

    def spy(cmd, *a, **kw):
        calls.append(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(validate_node.subprocess, "run", spy)
    for name in ("t1", "t2"):
        _npm_project(tmp_path / name / "web")
        validate_patch_tree(_Repo(), tmp_path / name, ["web/src/a.ts"], cache_dir=tmp_path / "v",
                            node_cache_dir=tmp_path / "node")
    assert sum(1 for c in calls if c[:2] == ["npm", "ci"]) == 1


def test_npm_never_uses_an_unwritable_home_cache(tmp_path: Path, monkeypatch) -> None:
    """In the container HOME is ``/``, so npm's default cache ``/.npm`` fails every install (EACCES)."""
    monkeypatch.setenv("HOME", "/")
    monkeypatch.delenv("npm_config_cache", raising=False)
    envs: list[dict] = []

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["npm", "ci"]:
            envs.append(kw.get("env") or {})
        raise subprocess.CalledProcessError(1, cmd, "", "stop")

    monkeypatch.setattr(validate_node.subprocess, "run", fake_run)
    root = tmp_path / "web"
    root.mkdir()
    (root / "package.json").write_text("{}")
    (root / "package-lock.json").write_text("{}")
    validate_node.ensure_node_modules(root, tmp_path / "cache")
    assert envs[0]["npm_config_cache"] == str((tmp_path / "cache" / ".npm-cache").resolve())
    assert validate_node._env()["npm_config_cache"] != "/.npm"


@needs_npm
def test_mixed_patch_runs_both_sides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (Path(sys.executable), ""))
    _npm_project(tmp_path / "web")
    _write(tmp_path, {"svc/requirements.txt": "", "svc/app/a.py": "X = 1\n", "svc/tests/test_a.py": "def test_a(): pass\n"})
    r = validate_patch_tree(_Repo(), tmp_path, ["svc/app/a.py", "web/src/a.ts"], cache_dir=tmp_path / "v",
                            node_cache_dir=tmp_path / "node")
    assert r.passed and r.tool == "pytest+npm_checks", r.raw_output
    assert "tests passed: svc" in r.raw_output and "npm checks passed: web" in r.raw_output


@needs_npm
def test_plain_js_outside_a_project_gets_a_syntax_check(tmp_path: Path) -> None:
    _write(tmp_path, {"static/ui.js": "export function f() { return 1; }\n"})
    r = validate_patch_tree(_Repo(), tmp_path, ["static/ui.js"], cache_dir=tmp_path / "v")
    assert r.passed and r.tool == "node_check", r.raw_output
    _write(tmp_path, {"static/ui.js": "function ( {\n"})
    r = validate_patch_tree(_Repo(), tmp_path, ["static/ui.js"], cache_dir=tmp_path / "v")
    assert not r.passed and r.tool == "node_check" and "SyntaxError" in r.raw_output


def test_packs_suggest_the_npm_project_checks_for_a_ts_file(tmp_path: Path) -> None:
    from auditor.validator import suggest_test_commands

    _write(tmp_path, {"pyproject.toml": "[project]\nname='x'\n"})  # a Python repo root must not win
    _npm_project(tmp_path / "web")
    tool, cmds = suggest_test_commands(tmp_path, "web/src/a.ts")
    assert tool == "npm_checks"
    assert cmds == ["cd web && npm ci", "cd web && npm run -s typecheck",
                    "cd web && npm run -s test -- --passWithNoTests"]  # the fixture's script says "vitest"
    assert suggest_test_commands(tmp_path, "app/x.py")[0] == "pytest"


def test_node_test_runner_failures_are_keyed_by_name_not_timing(tmp_path: Path) -> None:
    # node --test's TAP: each run's duration_ms differs, so reading its lines as ids made every run "new".
    def run(ms: str) -> str:
        return ("# Subtest: iceland: bundle matches\nnot ok 4 - iceland: bundle matches\n  ---\n"
                f"  duration_ms: {ms}\n  location: '{tmp_path}/test/parity.test.mjs:51:3'\n  ...\n"
                "ok 2 - fits Pages # SKIP\n    not ok 1 - inner case\nnot ok 5 - later # TODO not yet\n"
                f"1..5\n# fail 2\n# duration_ms {ms}\n")
    assert error_ids("test", run("0.14"), tmp_path) == error_ids("test", run("41.26"), tmp_path)
    assert sorted(error_ids("test", run("0.14"), tmp_path)) == ["iceland: bundle matches", "inner case"]
