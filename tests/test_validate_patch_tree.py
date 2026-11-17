"""validate_patch on a clean tree, per project: lint the changed files, run each project's tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import validator
from auditor.audit_api import app  # imported here: it loads .env, which the conftest then overrides
from auditor.validator import project_root, validate_patch_tree


class _Repo:
    name = "mono"
    path = ""


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@pytest.fixture
def host_python(monkeypatch):
    """Use this interpreter (it has pytest) instead of building per-project venvs."""
    built: list[Path] = []

    def fake(root, cache_dir):
        built.append(root)
        return Path(sys.executable), ""

    monkeypatch.setattr(validator, "ensure_test_venv", fake)
    return built


def test_project_root_finds_the_nearest_project(tmp_path: Path) -> None:
    _write(tmp_path, {"svc/requirements.txt": "", "svc/app/x.py": "", "tools/tests/test_t.py": "",
                      "tools/y.py": "", "loose/z.py": ""})
    assert project_root(tmp_path, "svc/app/x.py") == tmp_path / "svc"
    assert project_root(tmp_path, "tools/y.py") == tmp_path / "tools"  # a tests/ dir marks it too
    assert project_root(tmp_path, "loose/z.py").resolve() == tmp_path.resolve()


def test_requirement_lines_inline_includes_and_skip_self_install(tmp_path: Path) -> None:
    _write(tmp_path, {"requirements.txt": "fastapi>=0.1  # web\n-r base.txt\n-e .\n.\n-c constraints.txt\n",
                      "base.txt": "httpx\n# comment\n"})
    assert validator._requirement_lines(tmp_path / "requirements.txt") == ["fastapi>=0.1", "httpx"]


def test_runs_only_the_touched_projects_tests(tmp_path: Path, host_python) -> None:
    _write(tmp_path, {
        "svc/requirements.txt": "",
        "svc/app/calc.py": "def add(a, b):\n    return a + b\n",
        "svc/tests/test_calc.py": "from app.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "other/requirements.txt": "",
        "other/tests/test_broken.py": "def test_x():\n    assert False\n",
    })
    r = validate_patch_tree(_Repo(), tmp_path, ["svc/app/calc.py"])
    assert r.passed and r.tool == "pytest"
    assert "tests passed: svc" in r.raw_output
    assert host_python == [tmp_path / "svc"]


def test_failing_tests_fail(tmp_path: Path, host_python) -> None:
    _write(tmp_path, {
        "svc/requirements.txt": "",
        "svc/app/calc.py": "def add(a, b):\n    return a - b\n",
        "svc/tests/test_calc.py": "from app.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    })
    r = validate_patch_tree(_Repo(), tmp_path, ["svc/app/calc.py"])
    assert not r.passed and r.tool == "pytest"
    assert "== svc" in r.raw_output and "assert" in r.raw_output


def test_failures_already_on_the_base_branch_dont_count(tmp_path: Path, host_python) -> None:
    files = {
        "svc/requirements.txt": "",
        "svc/app/calc.py": "def add(a, b):\n    return a + b\n",
        "svc/tests/test_calc.py": "from app.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "svc/tests/test_flaky.py": "def test_known_broken():\n    assert False\n",
    }
    base, patched = tmp_path / "base", tmp_path / "patched"
    _write(base, files)
    _write(patched, {**files, "svc/app/calc.py": "def add(a, b):\n    \"\"\"Sum.\"\"\"\n    return a + b\n"})
    r = validate_patch_tree(_Repo(), patched, ["svc/app/calc.py"], base_tree=lambda: base)
    assert r.passed and r.tool == "pytest", r.raw_output
    assert "fail on the base branch too" in r.raw_output and "test_known_broken" in r.raw_output

    # a failure the patch introduces still fails, and is named
    _write(patched, {"svc/app/calc.py": "def add(a, b):\n    return a - b\n"})
    r = validate_patch_tree(_Repo(), patched, ["svc/app/calc.py"], base_tree=lambda: base)
    assert not r.passed
    assert "== svc\nnew failures from this patch: tests/test_calc.py::test_add\n" in r.raw_output


def test_file_outside_any_project_gets_lint_only(tmp_path: Path, host_python) -> None:
    # monorepo root without a project; the file's service dir has no marker either
    _write(tmp_path, {"svc/requirements.txt": "", "voice/server.py": "X = 1\n"})
    r = validate_patch_tree(_Repo(), tmp_path, ["voice/server.py"])
    assert r.passed and r.tool == "lint_only" and "no project" in r.raw_output
    assert host_python == []


def test_lint_failure_stops_before_tests(tmp_path: Path, host_python) -> None:
    _write(tmp_path, {"svc/requirements.txt": "", "svc/app/bad.py": "def f(:\n    pass\n"})
    r = validate_patch_tree(_Repo(), tmp_path, ["svc/app/bad.py"])
    assert not r.passed and r.tool == "lint_only"
    assert host_python == []


@pytest.mark.parametrize("files,note", [
    ({"svc/app/a.py": "X = 1\n"}, "no tests collected"),
    ({"svc/app/a.py": "X = 1\n", "svc/tests/test_a.py": "import not_a_real_module_xyz\n\ndef test_a():\n    pass\n"},
     "tests need a module the env lacks"),
])
def test_lint_stands_when_no_tests_can_run(tmp_path: Path, host_python, files, note) -> None:
    _write(tmp_path, {"svc/requirements.txt": "", **files})
    r = validate_patch_tree(_Repo(), tmp_path, ["svc/app/a.py"])
    assert r.passed and r.tool == "lint_only"
    assert "lint passed; no tests ran" in r.raw_output and note in r.raw_output


def test_env_build_failure_falls_back_to_lint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache: (None, "No matching distribution"))
    _write(tmp_path, {"svc/requirements.txt": "nope==9", "svc/app/a.py": "X = 1\n"})
    r = validate_patch_tree(_Repo(), tmp_path, ["svc/app/a.py"])
    assert r.passed and r.tool == "lint_only" and "test env did not build" in r.raw_output


def test_non_python_patch_uses_repo_validator(tmp_path: Path, monkeypatch) -> None:
    from auditor.contracts import ValidationResult

    monkeypatch.setattr(validator, "validate_repo", lambda repo, path_override=None, timeout_s=120:
                        ValidationResult(passed=True, tool="npm", duration_ms=1, errors=[], raw_output=""))
    _write(tmp_path, {"README.md": "x"})
    assert validate_patch_tree(_Repo(), tmp_path, ["README.md"]).tool == "npm"


def test_missing_interpreter_is_an_env_gap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache: (Path("no/such/python"), ""))
    _write(tmp_path, {"svc/requirements.txt": "", "svc/app/a.py": "X = 1\n", "svc/tests/test_a.py": "def test_a(): pass\n"})
    r = validate_patch_tree(_Repo(), tmp_path, ["svc/app/a.py"])
    assert r.passed and r.tool == "lint_only" and "lacks" in r.raw_output


def test_ensure_test_venv_returns_an_absolute_path(tmp_path: Path, monkeypatch) -> None:
    def fake_run(cmd, **kw):
        if cmd[1:3] == ["-m", "venv"]:
            (Path(cmd[3]) / "bin").mkdir(parents=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(validator.subprocess, "run", fake_run)
    monkeypatch.setattr(validator, "proc_run", fake_run)
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"svc/requirements.txt": ""})
    py, _ = validator.ensure_test_venv(tmp_path / "svc", Path("reports/_validate_venvs"))
    assert py.is_absolute()


def test_self_referencing_extras_expand_instead_of_installing_the_published_package(tmp_path: Path) -> None:
    # argus-mask's dev extra is "argus-mask[server,cli]": passed to pip as is, it pulled argus-mask
    # 0.2.0 from PyPI, whose argus-cortex<0.6 pin conflicted with the patched >=0.9 (lint only).
    _write(tmp_path, {"pyproject.toml": (
        '[project]\nname = "Argus_Mask"\ndependencies = ["argus-cortex>=0.9,<0.10"]\n'
        '[project.optional-dependencies]\n'
        'server = ["fastapi>=0.110", "argus-cortex[server]>=0.9,<0.10"]\n'
        'cli = ["typer>=0.12", "argus-mask[server]"]\n'
        'dev = ["argus-mask[server, cli]", "pytest>=8.0", "argus-mask[dev]"]\n')})
    _, specs = validator._declared_deps(tmp_path)
    assert not [s for s in specs if s.lower().startswith(("argus-mask", "argus_mask"))]
    assert specs == ["argus-cortex>=0.9,<0.10", "fastapi>=0.110", "argus-cortex[server]>=0.9,<0.10",
                     "typer>=0.12", "pytest>=8.0"]  # expanded once each, the self-cycle cut


def test_ci_test_extras_are_installed_too(tmp_path: Path) -> None:
    # argus-forge's tests import typer (cli) and fastapi (server); its CI installs
    # ".[dev,server,cli]" via argus-ci's python-ci.yml, but the venv only had dev (lint only).
    _write(tmp_path, {
        "pyproject.toml": ('[project]\nname = "argus-forge"\ndependencies = ["pydantic>=2.7"]\n'
                           '[project.optional-dependencies]\n'
                           'server = ["fastapi>=0.110"]\ncli = ["typer>=0.12"]\ngpu = ["torch"]\n'
                           'dev = ["pytest>=8.0"]\n'),
        ".github/workflows/ci.yml": ("jobs:\n  python:\n    uses: smk762/argus-ci/.github/workflows/python-ci.yml@v1\n"
                                     "    with:\n      test-extras: \"dev,server,cli\"  # tests import typer\n"),
    })
    assert validator.ci_test_extras(tmp_path) == ["dev", "server", "cli"]
    _, specs = validator._declared_deps(tmp_path)
    assert specs == ["pydantic>=2.7", "pytest>=8.0", "fastapi>=0.110", "typer>=0.12"]  # not gpu

    # A subproject finds its repo's workflows; a tree with none gets the default extras only.
    _write(tmp_path, {"svc/pyproject.toml": '[project]\nname = "svc"\n'})
    assert validator.ci_test_extras(tmp_path / "svc") == ["dev", "server", "cli"]
    bare = tmp_path / "bare"
    _write(bare, {".git/HEAD": "", "pyproject.toml": '[project]\nname = "bare"\n'})
    assert validator.ci_test_extras(bare) == []


def test_venv_build_uses_a_writable_pip_cache(tmp_path: Path, monkeypatch) -> None:
    envs: list[dict] = []

    def fake_run(cmd, **kw):
        if cmd[1:3] == ["-m", "venv"]:
            (Path(cmd[3]) / "bin").mkdir(parents=True)
        elif "pip" in cmd:
            envs.append(kw.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(validator.subprocess, "run", fake_run)
    monkeypatch.setattr(validator, "proc_run", fake_run)
    monkeypatch.setenv("HOME", "/")
    _write(tmp_path, {"svc/requirements.txt": "httpx\n"})
    validator.ensure_test_venv(tmp_path / "svc", tmp_path / "cache")
    assert envs[0]["PIP_CACHE_DIR"] == str((tmp_path / "cache" / ".pip-cache").resolve())


def test_ensure_test_venv_builds_once_per_deps(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["-m", "venv"]:
            (Path(cmd[3]) / "bin").mkdir(parents=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(validator.subprocess, "run", fake_run)
    monkeypatch.setattr(validator, "proc_run", fake_run)
    proj = tmp_path / "svc"
    _write(proj, {"requirements.txt": "httpx\n-e .\n",
                  "pyproject.toml": '[project]\nname="s"\ndependencies=["fastapi"]\n'
                                    '[project.optional-dependencies]\ntest=["respx"]\ndocs=["mkdocs"]\n'})
    cache = tmp_path / "cache"
    py, err = validator.ensure_test_venv(proj, cache)
    assert err == "" and py.name == "python" and len(calls) == 2
    reqs = Path(calls[1][calls[1].index("-r") + 1]).read_text().split()
    assert reqs == ["pytest", "httpx", "fastapi", "respx"]  # no self-install, no docs extra
    assert validator.ensure_test_venv(proj, cache) == (py, "") and len(calls) == 2  # cached
    _write(proj, {"requirements.txt": "httpx\nrich\n"})
    validator.ensure_test_venv(proj, cache)
    assert len(calls) == 4  # deps changed: new env


# ── endpoint ─────────────────────────────────────────────────────────────────

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_endpoint_validates_the_committed_tree_not_the_live_checkout(tmp_path: Path, monkeypatch, host_python) -> None:
    repo = tmp_path / "mono"
    _write(repo, {"svc/requirements.txt": "", "svc/app/calc.py": "def add(a, b):\n    return a + b\n",
                  "svc/tests/test_calc.py": "from app.calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"})
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    # uncommitted work in the live checkout that the patch would clash with
    (repo / "svc/app/calc.py").write_text("def add(a, b):\n    return 0  # wip\n", encoding="utf-8")

    class _RepoCfg:
        name = "mono"
        path = str(repo)

    class _Cfg:
        output_dir = str(tmp_path / "reports")
        auth_mode = False
        auth_code_env = "AI_AUDIT_AUTH_CODE"
        repos = [_RepoCfg()]

    monkeypatch.setattr("auditor.audit_api.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setattr("auditor.repo_resolver.resolve_repo_path", lambda r: Path(r.path))
    patch = ("--- a/svc/app/calc.py\n+++ b/svc/app/calc.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n"
             "+    \"\"\"Sum.\"\"\"\n     return a + b\n")
    resp = TestClient(app).post("/audit/validate_patch", json={"repo": "mono", "patch": patch})
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["patch_applied"] and body["base_ref"] == "HEAD"
    assert body["passed"] and body["tool"] == "pytest", body
    assert host_python == [host_python[0]] and host_python[0].name == "svc"
    assert "wip" in (repo / "svc/app/calc.py").read_text()  # live checkout untouched


def test_hatch_vcs_version_file_is_stubbed(tmp_path: Path, host_python) -> None:
    """A gitignored, build-generated _version.py must not push validation to lint-only."""
    _write(tmp_path, {
        "pyproject.toml": '[project]\nname = "pkg"\n\n[tool.hatch.build.hooks.vcs]\n'
                          'version-file = "src/pkg/_version.py"\n',
        "src/pkg/__init__.py": "from pkg._version import __version__\n",
        "src/pkg/core.py": "def one():\n    return 1\n",
        "tests/test_core.py": "import pkg\nfrom pkg.core import one\n\n"
                              "def test_one():\n    assert one() == 1 and pkg.__version__\n",
    })
    res = validate_patch_tree(_Repo(), tmp_path, ["src/pkg/core.py"], timeout_s=60)
    assert res.passed and res.tool == "pytest", res.raw_output
    assert "stub" in res.raw_output and (tmp_path / "src/pkg/_version.py").exists()


def test_version_stub_never_escapes_the_project(tmp_path: Path) -> None:
    _write(tmp_path, {"proj/pyproject.toml": '[tool.setuptools_scm]\nwrite_to = "../outside.py"\n'})
    assert validator.stub_version_files(tmp_path / "proj") == []
    assert not (tmp_path / "outside.py").exists()
