"""Regression tests for auditor/validator.py audit packs (batch 5)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from auditor import validator
from auditor.contracts import ValidationResult


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class _Repo:
    name = "mono"
    path = ""


@pytest.mark.parametrize("why", ["no_tests", "env_failed"])
def test_a_deletion_only_patch_with_no_tests_is_skipped_not_passed(tmp_path: Path, monkeypatch, why) -> None:
    # 9b0d252e81: every changed .py was deleted, so nothing was linted; with no tests either, no check ran.
    _write(tmp_path, {"pyproject.toml": "[project]\nname='a'\n"})
    if why == "no_tests":
        monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (Path(sys.executable), ""))
        monkeypatch.setattr(validator, "_run_project_tests", lambda *a, **k: (
            "no_tests", ValidationResult(passed=True, tool="pytest", duration_ms=0, errors=[], raw_output="")))
    else:
        monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (None, "pip failed"))
    monkeypatch.setattr(validator, "validate_repo", lambda *a, **k: pytest.fail("generic validation used"))
    res = validator.validate_patch_tree(_Repo(), tmp_path, ["a/mod.py"], cache_dir=tmp_path / "cache")
    assert not res.passed
    assert res.tool == "skipped" and "a/mod.py" in (res.skipped_reason or "")
    assert "lint passed" not in res.raw_output


@pytest.mark.parametrize(("cfg", "want"), [
    ("home = /x\nversion_info = 3.10.12.final.0\nversion = 3.10.12\n", "3.10.12"),
    ("version_info = 3.12.4.final.0\n", "3.12.4"),
    ("version = 3.11\n", "3.11"),
], ids=["virtualenv", "info_only", "two_part"])
def test_venv_python_version_is_major_minor_patch(tmp_path: Path, cfg, want) -> None:  # e642ce1d72
    (tmp_path / "bin").mkdir()
    (tmp_path / "pyvenv.cfg").write_text(cfg)
    assert validator.venv_python_version(tmp_path / "bin" / "python") == want


@pytest.mark.parametrize("line", [".", "./", "-e .", "-e.", "-e ./", "--editable .", "--editable=.",
                                  "--editable=./", ".[dev]", "-e .[dev,test]", "-cconstraints.txt",
                                  "-c/etc/passwd", "-c constraints.txt", "--constraint=c.txt", "--constraint c.txt"],
                         ids=[f"line{i}" for i in range(15)])
def test_self_installs_and_constraints_are_dropped_in_any_spelling(tmp_path: Path, line) -> None:  # 6235bb7413
    _write(tmp_path, {".git/HEAD": "", "requirements.txt": f"httpx\n{line}\n"})
    assert validator._requirement_lines(tmp_path / "requirements.txt") == ["httpx"]


def test_ordinary_requirements_are_kept(tmp_path: Path) -> None:
    _write(tmp_path, {".git/HEAD": "", "requirements.txt": "-e git+https://x/y.git#egg=y\ncolorama\n./sub\n"})
    assert validator._requirement_lines(tmp_path / "requirements.txt") == [
        "-e git+https://x/y.git#egg=y", "colorama", "./sub"]


@pytest.mark.parametrize(("spec", "want"), [
    ("==3.11.4", "3.11"),
    (">=3.10.2,<3.10.8", "3.10"),
    ("==3.12.150", "3.12"),
], ids=["exact_pin", "bounded_range", "patch_over_99"])
def test_a_spec_met_only_by_mid_range_patches_still_fits(tmp_path: Path, spec, want) -> None:  # 19f3507c86
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "p"\nversion = "0"\nrequires-python = "{spec}"\n')
    assert validator.test_python(tmp_path, "3.13") == (want, "")
