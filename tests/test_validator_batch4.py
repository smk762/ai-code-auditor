"""Regression tests for auditor/validator.py audit packs (batch 4)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from auditor import validator
from auditor.contracts import ValidationResult


def _write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text if isinstance(text, bytes) else text.encode())


class _Repo:
    name = "mono"
    path = ""


def test_pytest_section_headers_and_verdict_survive(tmp_path: Path) -> None:  # c094fb6791
    out = ("============ FAILURES ============\n"
           "------ Captured stdout call ------\n"
           "==========\n"
           "======= 2 failed, 10 passed in 1.23s =======\n")
    kept = [ln for ln in out.splitlines() if not validator._is_boilerplate(ln)]
    assert kept == ["============ FAILURES ============", "------ Captured stdout call ------",
                    "======= 2 failed, 10 passed in 1.23s ======="]


def test_passed_line_with_errors_or_warnings_is_kept() -> None:  # 579e2eaffe
    assert not validator._is_boilerplate("3 passed, 1 error in 0.4s")
    assert not validator._is_boilerplate("5 passed, 2 warnings in 0.1s")
    assert not validator._is_boilerplate("4 passed, 1 xpassed in 0.1s")
    assert validator._is_boilerplate("12 passed in 0.31s")
    assert validator._is_boilerplate("12 passed, 2 skipped in 0.31s")
    assert validator._is_boilerplate("===== 12 passed in 0.31s =====")


def test_composite_tools_use_each_parts_parser() -> None:  # 3c2f0ac7eb
    out = "--- FAIL: TestAdd (0.00s)\nFAIL\tgithub.com/x/calc\t0.01s\nFAILED tests/test_a.py::test_a - boom\n"
    summary = validator.extract_failure_summary(out, "pytest+go_test")
    assert "--- FAIL: TestAdd (0.00s)" in summary and "FAILED tests/test_a.py::test_a - boom" in summary
    out = "thread 'it_adds' panicked at src/lib.rs:3:5\nFAILED tests/test_b.py::test_b - x\n"
    assert any("panicked" in ln for ln in validator.extract_failure_summary(out, "pytest+cargo_test"))
    out = "    Expected: 5\nFAILED tests/test_c.py::test_c - y\n"
    assert "    Expected: 5" in validator.extract_failure_summary(out, "pytest+npm_test")


def test_non_utf8_workflow_is_read_not_fatal(tmp_path: Path) -> None:  # 33c328cb85
    _write(tmp_path, {".git/HEAD": "", ".github/workflows/ci.yml": b"# caf\xe9\nrun: pip install pyyaml\n",
                      ".github/workflows/z.yml": "run: uv run --with httpx pytest\n"})
    assert sorted(validator.ci_tool_deps(tmp_path)) == ["httpx", "pyyaml"]


def test_values_of_pip_options_are_not_packages(tmp_path: Path) -> None:  # 08286bd435
    _write(tmp_path, {".git/HEAD": "", ".github/workflows/ci.yml":
                      "run: pip install --target vendor -i https://x/simple --platform manylinux2014_x86_64 "
                      "--only-binary none --extra-index-url https://y pyyaml\n"})
    assert validator.ci_tool_deps(tmp_path) == ["pyyaml"]


def test_requirement_include_cycles_are_read_once(tmp_path: Path) -> None:  # c5e41855cc
    _write(tmp_path, {".git/HEAD": "",
                      "requirements.txt": "httpx\n" + "-r requirements.txt\n" * 8 + "-r a.txt\n-r b.txt\n",
                      "a.txt": "-r b.txt\n-r requirements.txt\nfastapi\n", "b.txt": "-r a.txt\nrich\n"})
    reads: list[Path] = []
    real = Path.read_text

    def counting(self, *a, **k):
        reads.append(self)
        return real(self, *a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "read_text", counting)
        lines = validator._requirement_lines(tmp_path / "requirements.txt")
    assert sorted(lines) == ["fastapi", "httpx", "rich"]
    assert len(reads) == 3


def test_patch_that_only_deletes_py_files_tests_their_project(tmp_path: Path, monkeypatch) -> None:  # 74ccd2b831
    _write(tmp_path, {"pyproject.toml": "[project]\nname='a'\n", "tests/test_a.py": "from a import mod\n"})
    tested: list[Path] = []
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (Path(sys.executable), ""))

    def fake_tests(root, python, timeout_s, only=None):
        tested.append(root)
        return "ran", ValidationResult(passed=False, tool="pytest", duration_ms=1, errors=["ImportError"],
                                       raw_output="FAILED tests/test_a.py::test_x - ImportError\n1 failed in 0.1s\n")

    monkeypatch.setattr(validator, "_run_project_tests", fake_tests)
    monkeypatch.setattr(validator, "validate_repo", lambda *a, **k: ValidationResult(
        passed=True, tool="lint_only", duration_ms=0, errors=[], raw_output="generic"))
    res = validator.validate_patch_tree(_Repo(), tmp_path, ["a/mod.py"], cache_dir=tmp_path / "cache")
    assert tested == [tmp_path.resolve()]
    assert not res.passed and res.tool == "pytest"  # the removal broke an import: not a lint pass
