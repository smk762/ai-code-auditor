"""Regression tests for auditor/evidence.py audit packs (batch 4)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from auditor import evidence


def test_teardown_error_is_not_hidden_by_a_later_passed_line() -> None:  # e964bfd893
    out = ("ERROR tests/test_a.py::test_x - RuntimeError: teardown boom\n"
           "PASSED tests/test_a.py::test_x\n"
           "PASSED tests/test_a.py::test_y\n"
           "SKIPPED tests/test_a.py::test_z\n"
           "FAILED tests/test_a.py::test_z - AssertionError\n")
    got = evidence.parse_pytest_outcomes(out)
    assert got == {"tests/test_a.py::test_x": "error", "tests/test_a.py::test_y": "passed",
                   "tests/test_a.py::test_z": "failed"}


def test_patch_file_names_are_never_pytest_options(tmp_path: Path, monkeypatch) -> None:  # ced9ef2d10
    seen: list[list[str]] = []
    monkeypatch.setattr(evidence, "proc_run", lambda cmd, **kw: seen.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    files = ["-pevil/test_a.py", "--rootdir=/x_test.py", "tests/test_b.py"]
    evidence._pytest(tmp_path, Path(sys.executable), files, 30)
    cmd = seen[0]
    assert "--" in cmd and cmd[cmd.index("--") + 1:] == files


def test_option_like_test_file_runs_as_a_file(tmp_path: Path) -> None:  # ced9ef2d10
    # end to end: pytest accepts the `--` separator and still runs an option-like file name
    (tmp_path / "-k_test.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    outcomes, out = evidence._pytest(tmp_path, Path(sys.executable), ["-k_test.py"], 60)
    assert outcomes == {"-k_test.py::test_ok": "passed"}, out
