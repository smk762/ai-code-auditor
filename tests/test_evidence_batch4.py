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


# ── unittest subTest failures (pytest >= 9 lists the test itself as PASSED) ──

def test_a_failing_subtest_fails_its_test() -> None:
    out = ("PASSED tests/test_a.py::T::test_sub\n"
           "PASSED tests/test_a.py::T::test_ok\n"
           "SUBFAILED[case with spaces] (i=1) tests/test_a.py::T::test_sub - AssertionError: 1 == 1\n"
           "SUBFAILED(i=2) tests/test_a.py::test_native[p1] - assert 2 == 0\n"
           "FAILED tests/test_a.py::test_native[p1] - contains 1 failed subtest\n"
           "SUBFAIL tests/test_a.py::test_old_plugin - assert 0\n")
    assert evidence.parse_pytest_outcomes(out) == {
        "tests/test_a.py::T::test_sub": "failed", "tests/test_a.py::T::test_ok": "passed",
        "tests/test_a.py::test_native[p1]": "failed", "tests/test_a.py::test_old_plugin": "failed"}


_SUBTEST_FILE = '''import unittest

LIMIT = {limit}


class T(unittest.TestCase):
    def test_sub(self):
        for i in range(3):
            with self.subTest("each value", i=i):
                self.assertLess(i, LIMIT)
'''


def test_subtest_failure_is_failed_end_to_end_and_shows_repro(tmp_path: Path) -> None:
    # the validator's own pytest, whatever its version or plugins: a failing subtest must read as failed
    base, patched = tmp_path / "base", tmp_path / "patched"
    for tree, limit in ((base, 2), (patched, 3)):
        tree.mkdir()
        (tree / "test_sub.py").write_text(_SUBTEST_FILE.format(limit=limit), encoding="utf-8")
    on_dev, out = evidence._pytest(base, Path(sys.executable), ["test_sub.py"], 60)
    assert on_dev == {"test_sub.py::T::test_sub": "failed"}, out
    with_fix, out = evidence._pytest(patched, Path(sys.executable), ["test_sub.py"], 60)
    assert with_fix == {"test_sub.py::T::test_sub": "passed"}, out
    assert evidence.ReproResult(patched=with_fix, base=on_dev).repro == ["test_sub.py::T::test_sub"]
