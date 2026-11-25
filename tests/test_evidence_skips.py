"""validate_patch records what the tests skipped; a skipped test covers nothing (auditor/validator.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from auditor import evidence, validator
from auditor.config import RepoConfig

MIXED = ("import pytest\n\n\n@pytest.mark.skipif(True, reason='GDAL not installed')\n"
         "@pytest.mark.parametrize('i', range(3))\ndef test_a(i): pass\n\n\n"
         "def test_b(): pytest.skip('no network')\n\n\ndef test_c(): pass\n")


@pytest.fixture()
def run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (Path(sys.executable), ""))
    tree = tmp_path / "t"
    (tree / "tests").mkdir(parents=True)
    (tree / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    (tree / "m.py").write_text("x = 1\n")

    def go(files: dict[str, str]):
        for name, text in files.items():
            (tree / "tests" / name).write_text(text)
        return validator.validate_patch_tree(RepoConfig(name="x", path=str(tree)), tree, ["m.py"], cache_dir=tmp_path / "c")
    return go


def test_skips_are_counted_by_reason_and_file(run) -> None:
    r = run({"test_x.py": MIXED})
    assert r.passed and (r.tests["passed"], r.tests["skipped"], r.tests["failed"]) == (1, 4, 0)
    assert r.tests["skip_reasons"] == {"GDAL not installed": 3, "no network": 1}
    assert r.tests["skipped_files"] == {"tests/test_x.py": 4}
    assert "4 test(s) skipped, 1 passed" in r.raw_output and "GDAL not installed (3)" in r.raw_output


def test_a_run_with_no_skips_says_nothing_about_them(run) -> None:
    r = run({"test_x.py": "def test_c(): pass\n"})
    assert r.tests["skipped"] == 0 and "skipped" not in r.raw_output


def test_a_skip_for_a_missing_module_is_not_an_env_gap(run) -> None:
    """Collection fails on the patch's own error: a skip that names a missing module must not excuse it."""
    r = run({"test_skip.py": "import pytest\npytest.importorskip('nope_nope')\n", "test_bad.py": "raise RuntimeError('broken')\n"})
    assert r.passed is False and "RuntimeError" in r.raw_output


def test_failures_survive_many_skips(run) -> None:
    many = "import pytest\n" + "".join(f"def test_{i}(): pytest.skip('reason {i} {'x' * 80}')\n" for i in range(200))
    r = run({"test_many.py": many, "test_f.py": "def test_f(): assert 0\n"})
    assert r.passed is False and "FAILED tests/test_f.py::test_f" in r.raw_output and "SKIPPED" not in r.raw_output
    assert r.tests["skipped"] == 200 and len(r.tests["skip_reasons"]) == 10


def test_output_with_no_summary_is_unknown_not_zero() -> None:
    assert validator.pytest_counts("Traceback …") == {} and validator.merge_counts([{}, {}]) == {}
    one = validator.pytest_counts("2 passed, 1 skipped in 0.10s")
    assert validator.merge_counts([one, {}])["partial"] is True
    assert validator.merge_counts([one, one])["passed"] == 4


PACKS = [({"kind": "architecture"}, ["a.py"], "tests"), ({"kind": "architecture"}, ["tests/test_a.py"], "test_only"),
         ({"kind": "module_cycle"}, ["a.py"], "structure")]


def _decide(pack: dict, changed: list[str], summary: str | None, tool: str = "pytest", **extra):
    tests = None if summary is None else {**validator.pytest_counts(summary), **extra}
    return evidence.decide(pack, tool=tool, changed=changed, repro=None, tests=tests,
                           structure={"gone": True, "reason": "gone"} if pack["kind"] == "module_cycle" else None)


@pytest.mark.parametrize(("pack", "changed", "kind"), PACKS)
def test_a_run_where_nothing_passed_is_unverified(pack: dict, changed: list[str], kind: str) -> None:
    for summary in ("76 skipped in 1.00s", "3 skipped, 1 xfailed in 1.00s"):
        d = _decide(pack, changed, summary)
        assert (d.status, d.kind) == ("unverified", "none") and "no test passed" in d.reason
    d = _decide(pack, changed, "1 passed, 76 skipped in 1.00s")
    assert (d.status, d.kind) == ("resolved", kind) and d.evidence["tests"]["skipped"] == 76


@pytest.mark.parametrize(("pack", "changed", "kind"), PACKS)
def test_unknown_counts_and_other_checks_still_count(pack: dict, changed: list[str], kind: str) -> None:
    assert _decide(pack, changed, None).kind == kind  # no counts at all
    assert _decide(pack, changed, "2 skipped in 1.00s", partial=True).kind == kind  # another run may have passed
    assert _decide(pack, changed, "2 skipped in 1.00s", tool="pytest+npm_checks").kind == kind


def test_an_all_skipped_run_takes_a_waiver_like_any_other_unverified_one() -> None:
    tests = validator.pytest_counts("2 skipped in 1.00s")
    d = evidence.decide({"kind": "architecture"}, tool="pytest", changed=["a.py"], repro=None, tests=tests,
                        waiver="needs a GPU the validator does not have")
    assert (d.status, d.kind) == ("resolved", "waiver")


def test_geo_tools_reach_a_validated_test_without_its_environment() -> None:
    """The wrappers name the data dirs themselves: child_env drops PROJ_DATA and GDAL_DATA."""
    root = Path(__file__).resolve().parents[1]
    wrap = (root / "docker/geo/wrap.sh").read_text()
    assert all(f"{v}=" in wrap for v in ("PROJ_DATA", "GDAL_DATA", "PROJ_NETWORK"))
    lock = [ln for ln in (root / "docker/geo/geo-linux-64.lock").read_text().splitlines() if ln.startswith("http")]
    assert lock and all(ln.startswith("https://conda.anaconda.org/conda-forge/") and "#" in ln for ln in lock)


def test_the_default_time_fits_a_suite_whose_tests_really_run() -> None:
    from auditor.audit_api import _ValidatePatchRequest

    assert _ValidatePatchRequest(repo="x", patch="").timeout_s >= 300  # atlas takes 96 s with GDAL, alone on the host
