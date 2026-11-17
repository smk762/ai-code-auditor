"""Test-env readiness: collection in validate_patch's envs, per project (auditor/test_env_readiness.py)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from auditor import test_env_readiness as ter
from auditor import validator


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin"})


@pytest.fixture
def host_python(monkeypatch):
    """Use this interpreter (it has pytest) instead of building per-project venvs."""
    built: list[Path] = []

    def fake(root, cache_dir):
        built.append(root)
        return Path(sys.executable), ""

    monkeypatch.setattr(validator, "ensure_test_venv", fake)
    return built


# One repo, three Python projects: one whose tests import, one whose tests need an undeclared
# module, one with no tests; plus files the check must not treat as projects.
TREE = {
    "good/pyproject.toml": "[project]\nname='good'\nversion='0'\n",
    "good/src/good/__init__.py": "def add(a, b):\n    return a + b\n",
    "good/tests/test_add.py": "from good import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                              "def test_add_zero():\n    assert add(0, 0) == 0\n",
    "gap/requirements.txt": "",
    "gap/app.py": "X = 1\n",
    "gap/tests/test_ok.py": "def test_ok():\n    assert True\n",
    "gap/tests/test_needs.py": "import not_a_declared_dependency_xyz\n\n\ndef test_x():\n    pass\n",
    "empty/requirements.txt": "",
    "empty/lib.py": "Y = 2\n",
    "good/tests/fixtures/proj/pyproject.toml": "[project]\nname='fixture'\n",
    "good/tests/fixtures/proj/m.py": "",
    "web/node_modules/dep/index.js": "",
}


def test_check_tree_classifies_each_project_as_validate_patch_would(tmp_path: Path, host_python) -> None:
    _write(tmp_path, TREE)
    res = {p["project"]: p for p in ter.check_tree(tmp_path, venv_cache=tmp_path / "v", node_cache=tmp_path / "n")}
    assert set(res) == {"good", "gap", "empty"}  # fixtures and node_modules are not projects
    assert res["good"]["status"] == "ready" and res["good"]["tests"] == 2
    gap = res["gap"]
    # one bad test module stops collection for the whole project, so every patch there is lint-only
    assert gap["status"] == "import_errors" and gap["missing"] == ["not_a_declared_dependency_xyz"]
    assert gap["error_files"] == ["tests/test_needs.py"]
    assert res["empty"]["status"] == "no_tests"
    assert {p.name for p in host_python} == {"good", "gap", "empty"}


def test_env_build_failure_is_reported_not_raised(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, {"svc/requirements.txt": "nope==0\n", "svc/tests/test_a.py": "def test_a():\n    pass\n"})
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache: (None, "No matching distribution for nope==0"))
    [p] = ter.check_tree(tmp_path, venv_cache=tmp_path / "v", node_cache=tmp_path / "n")
    assert p["status"] == "env_failed" and "nope==0" in p["error"]


def test_node_project_without_checks_or_lockfile(tmp_path: Path) -> None:
    _write(tmp_path, {"a/package.json": '{"scripts": {"build": "tsc"}}',
                      "b/package.json": '{"scripts": {"lint": "eslint ."}}'})
    res = {p["project"]: p for p in ter.check_tree(tmp_path, venv_cache=tmp_path / "v", node_cache=tmp_path / "n")}
    assert res["a"]["status"] == "no_checks"
    assert res["b"]["status"] == "env_failed" and "package-lock.json" in res["b"]["error"]


def test_parse_collect_names_missing_modules_and_cant_import() -> None:
    out = ("ERROR tests/test_a.py\nE   ModuleNotFoundError: No module named 'torch.nn'\n"
           "ERROR tests/test_b.py\nE   ImportError: cannot import name 'Thing' from 'argus_cortex'\n"
           "3 tests collected, 2 errors in 0.1s\n")
    p = ter.parse_collect(out, 2)
    assert p["status"] == "import_errors" and p["missing"] == ["argus_cortex (Thing)", "torch"]
    assert p["tests"] == 3 and p["error_files"] == ["tests/test_a.py", "tests/test_b.py"]
    assert ter.parse_collect("SyntaxError: bad\n1 error\n", 2)["status"] == "collect_errors"


def test_run_checks_the_dev_tip_never_the_checkout_and_writes_the_report(tmp_path: Path, host_python,
                                                                         monkeypatch) -> None:
    repo = tmp_path / "mono"
    _write(repo, TREE)
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    # an uncommitted change in the checkout must not be what gets checked
    (repo / "good/tests/test_add.py").write_text("import not_committed_xyz\n")

    class _Repo:
        name, path, enabled = "mono", str(repo), True

    class _Cfg:
        output_dir = str(tmp_path / "reports")
        repos = [_Repo()]

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setenv("AI_AUDIT_VALIDATE_FETCH", "0")
    rep = ter.run(_Cfg.output_dir)
    projects = {p["project"]: p["status"] for p in rep["repos"]["mono"]["projects"]}
    assert projects == {"good": "ready", "gap": "import_errors", "empty": "no_tests"}
    assert rep["summary"]["failing"] == {"mono": ["gap"]}
    assert ter.load_report(_Cfg.output_dir)["repos"]["mono"]["base_sha"]
    assert (repo / "good/tests/test_add.py").read_text() == "import not_committed_xyz\n"  # checkout untouched


def test_run_quietly_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(ter, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ter.run_quietly("x") == {"error": "boom"}
    monkeypatch.setenv("AI_AUDIT_TEST_ENV_CHECK", "0")
    assert "skipped" in ter.run_quietly("x")
