"""Regression tests for auditor/validator.py audit packs."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from auditor import validator
from auditor.contracts import ValidationResult
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


# ── batch 2 ──────────────────────────────────────────────────────────────────

class _Repo:
    name = "mono"
    path = ""


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _failing(out: str) -> ValidationResult:
    return ValidationResult(passed=False, tool="pytest", duration_ms=1, errors=["x"], raw_output=out)


def test_truncated_failure_list_never_becomes_a_pass(tmp_path: Path, monkeypatch) -> None:  # dfee834792
    # pytest counted 40 failures but the stored tail only shows one id, which also fails on base:
    # the other 39 were cut off and can't be compared, so the failure must stand.
    (tmp_path / "base").mkdir()
    patched = "FAILED tests/test_a.py::test_old - boom\n40 failed, 3 passed in 1.20s\n"
    base = "FAILED tests/test_a.py::test_old - boom\n1 failed, 42 passed in 1.10s\n"
    monkeypatch.setattr(validator, "_run_project_tests", lambda *a, **k: ("ran", _failing(base)))
    res = validator._against_base(tmp_path, tmp_path, lambda: tmp_path / "base", Path(sys.executable), 30,
                                  _failing(patched), ".", [])
    assert not res.passed
    # a complete list is still judged against base as before
    res = validator._against_base(tmp_path, tmp_path, lambda: tmp_path / "base", Path(sys.executable), 30,
                                  _failing(base), ".", [])
    assert res.passed


def test_non_utf8_tool_output_does_not_crash(tmp_path: Path) -> None:  # fab90fed3c
    code = "import sys; sys.stdout.buffer.write(b'caf\\xe9 \\xff\\n'); sys.stderr.buffer.write(b'\\xfe'); sys.exit(1)"
    res = validator._exec([sys.executable, "-c", code], cwd=tmp_path, tool="lint_only", timeout_s=30)
    assert not res.passed and "caf" in res.raw_output


def test_base_comparison_with_a_symlinked_tree(tmp_path: Path, monkeypatch) -> None:  # 1e7e56ae81
    real = tmp_path / "real"
    _write(real, {"svc/requirements.txt": "", "svc/app.py": ""})
    link = tmp_path / "link"
    link.symlink_to(real)
    root = validator.project_root(link, "svc/app.py")
    assert root == (real / "svc")
    (tmp_path / "base" / "svc").mkdir(parents=True)
    out = "FAILED tests/test_a.py::test_a - boom\n1 failed in 0.1s\n"
    monkeypatch.setattr(validator, "_run_project_tests", lambda *a, **k: ("ran", _failing(out)))
    res = validator._against_base(link / "svc", link, lambda: tmp_path / "base", Path(sys.executable), 30,
                                  _failing(out), "svc", [])
    assert res.passed  # fails on base too: compared, not crashed on relative_to


def test_env_gap_seen_beyond_the_stored_tail(tmp_path: Path, monkeypatch) -> None:  # 1f10194ad4
    stdout = ("ERROR tests/test_a.py\nE   ModuleNotFoundError: No module named 'torch'\n"
              + "".join(f"ERROR tests/test_{i}.py - collection failed with a long message\n" for i in range(400))
              + "!!! Interrupted: 401 errors during collection !!!\n")
    monkeypatch.setattr(validator, "proc_run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, stdout, ""))
    outcome, res = validator._run_project_tests(tmp_path, Path(sys.executable), 30)
    assert outcome == "env_gap"
    assert len(res.raw_output) <= validator._OUTPUT_LIMIT


def test_deleted_py_file_still_tests_its_project(tmp_path: Path, monkeypatch) -> None:  # 1f23d7fbe3
    _write(tmp_path, {"a/pyproject.toml": "[project]\nname='a'\n", "a/tests/test_a.py": "",
                      "b/pyproject.toml": "[project]\nname='b'\n", "b/tests/test_b.py": "", "b/x.py": "X = 1\n"})
    tested: list[Path] = []
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (Path(sys.executable), ""))

    def fake_tests(root, python, timeout_s, only=None):
        tested.append(root)
        return "ran", ValidationResult(passed=True, tool="pytest", duration_ms=1, errors=[], raw_output="ok")

    monkeypatch.setattr(validator, "_run_project_tests", fake_tests)
    linted: list[list[str]] = []
    monkeypatch.setattr(validator, "_lint_files", lambda tree, files, t: linted.append(files) or
                        ValidationResult(passed=True, tool="lint_only", duration_ms=1, errors=[], raw_output=""))
    validator.validate_patch_tree(_Repo(), tmp_path, ["a/mod.py", "b/x.py"], cache_dir=tmp_path / "cache")
    assert {p.name for p in tested} == {"a", "b"}
    assert linted == [["b/x.py"]]  # the deleted file isn't linted


def test_jest_expected_received_lines_are_kept() -> None:  # 2ae1af424c
    lines = ["    Expected: 5", '    Received: "foo"', "    at Object.<anonymous>"]
    assert validator._npm_summary(lines) == lines[:2]


def test_changed_file_names_are_never_lint_options(tmp_path: Path, monkeypatch) -> None:  # 31db570555
    seen: list[list[str]] = []
    monkeypatch.setattr(validator, "_exec", lambda cmd, **kw: seen.append(cmd) or
                        ValidationResult(passed=True, tool="lint_only", duration_ms=0, errors=[], raw_output=""))
    for ruff in ("/usr/bin/ruff", None):
        monkeypatch.setattr(validator.shutil, "which", lambda name, r=ruff: r)
        validator._lint_files(tmp_path, ["--fix.py", "-", "ok.py"], 30)
    for cmd in seen:
        files = cmd[-3:]
        assert files == ["./--fix.py", "./-", "ok.py"], cmd


def test_exec_error_falls_back_to_stdout(tmp_path: Path) -> None:  # 3de9a5121e
    code = "print('x.py:1:1: F821 undefined name'); raise SystemExit(1)"
    res = validator._exec([sys.executable, "-c", code], cwd=tmp_path, tool="lint_only", timeout_s=30)
    assert res.errors and "F821" in res.errors[0]


@pytest.mark.parametrize("toml", ['tool = "x"\n', "[tool]\nhatch = 1\n", "[tool.hatch.build]\nhooks = ['vcs']\n",
                                  "[tool]\nsetuptools_scm = 'x'\n", "[tool.hatch.build.hooks]\nvcs = 3\n"])
def test_non_table_pyproject_values_do_not_crash(tmp_path: Path, toml: str) -> None:  # 4e67045550
    (tmp_path / "pyproject.toml").write_text(toml, encoding="utf-8")
    assert validator.stub_version_files(tmp_path) == []


def test_write_probe_never_deletes_an_existing_file(tmp_path: Path) -> None:  # 833ebdfbd9
    keep = tmp_path / ".ai_audit_write_probe"
    keep.write_text("someone else's\n", encoding="utf-8")
    assert validator._is_writable(tmp_path)
    assert keep.read_text(encoding="utf-8") == "someone else's\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [".ai_audit_write_probe"]  # no probe left behind


def test_pytest_error_comes_from_stdout_not_stderr_noise(tmp_path: Path, monkeypatch) -> None:  # ab84be10a4
    stdout = "FAILED tests/test_a.py::test_a - AssertionError: 1 != 2\n1 failed in 0.1s\n"
    monkeypatch.setattr(validator, "proc_run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout, "DeprecationWarning: something at import\n"))
    _, res = validator._run_project_tests(tmp_path, Path(sys.executable), 30)
    assert "AssertionError: 1 != 2" in res.errors[0]


@pytest.mark.parametrize("pkg", ["[]", '"x"', '{"scripts": "jest"}', '{"scripts": ["jest"]}',
                                 '{"scripts": {"test": null}}', '{"scripts": {"test": 1}}'])
def test_odd_package_json_shapes_disable_pass_with_no_tests(tmp_path: Path, pkg: str) -> None:  # c377f20880
    (tmp_path / "package.json").write_text(pkg, encoding="utf-8")
    assert validator._node_runner_supports_pass_with_no_tests(tmp_path) is False


def test_hash_pinned_requirements_are_joined(tmp_path: Path) -> None:  # fdc58908ef
    (tmp_path / ".git").mkdir()
    (tmp_path / "requirements.txt").write_text(
        "httpx==0.27.0 \\\n    --hash=sha256:aaa \\\n    --hash=sha256:bbb\n"
        "# via\n#   app\nrich==13.0 --hash=sha256:ccc\nfastapi\n", encoding="utf-8")
    assert validator._requirement_lines(tmp_path / "requirements.txt") == ["httpx==0.27.0", "rich==13.0", "fastapi"]
