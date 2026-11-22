"""#141: a project's tests run on the interpreter its requires-python asks for."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from auditor import validator


def _proj(tmp_path: Path, spec: str | None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    body = '[project]\nname = "p"\nversion = "0"\n' + (f'requires-python = "{spec}"\n' if spec is not None else "")
    (tmp_path / "pyproject.toml").write_text(body)
    return tmp_path


@pytest.mark.parametrize(("spec", "here", "want"), [
    (None, "3.11", "3.11"),            # nothing declared: the image's interpreter, as before
    (">=3.11", "3.11", "3.11"),        # the image satisfies it: no download, same cache keys
    (">=3.12", "3.11", "3.12"),        # argus-scout: the oldest that satisfies it
    (">=3.11,<3.13", "3.13", "3.11"),  # argus-echo's Miso on a 3.13 host
    ("~=3.12.4", "3.11", "3.12"),      # a patch-level floor still picks the minor
    ("<3.12.5", "3.13", "3.9"),
    ("not a spec", "3.11", "3.11"),    # unreadable: keep the old behaviour rather than refuse
])
def test_test_python_picks_the_interpreter(tmp_path: Path, spec, here, want) -> None:
    assert validator.test_python(_proj(tmp_path, spec), here) == (want, "")


def test_no_supported_python_is_an_error(tmp_path: Path) -> None:
    minor, why = validator.test_python(_proj(tmp_path, "==2.7.*"), "3.11")
    assert minor == "" and "==2.7.*" in why


def test_a_project_the_image_cannot_run_builds_its_env_with_uv(tmp_path: Path, monkeypatch) -> None:
    root = _proj(tmp_path / "scout", ">=3.99")
    monkeypatch.setattr(validator, "TEST_PYTHONS", ("3.99",))
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        if cmd[1:3] == ["venv", "--seed"]:
            venv = Path(cmd[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text("home = /x\nversion_info = 3.99.1\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(validator, "proc_run", fake_run)
    monkeypatch.setattr(validator.subprocess, "run", lambda *a, **k: pytest.fail("python -m venv used"))
    python, err = validator.ensure_test_venv(root, tmp_path / "cache")
    assert err == "" and python is not None and validator.venv_python_version(python) == "3.99.1"
    uv_cmd, kw = calls[0]
    assert uv_cmd[1:5] == ["venv", "--seed", "--python", "3.99"]
    cache = (tmp_path / "cache").resolve()
    assert kw["env"]["UV_PYTHON_INSTALL_DIR"] == str(cache / ".uv-python")  # writable, never HOME
    assert "AI_AUDIT_API_TOKEN" not in kw["env"]                             # still the child allowlist
    assert calls[1][0][1:4] == ["-m", "pip", "install"]                      # the same install as ever
    # a second call reuses the env; a different interpreter would be a different env
    assert validator.ensure_test_venv(root, tmp_path / "cache")[0] == python and len(calls) == 2


def test_an_unsatisfiable_requires_python_builds_nothing(tmp_path: Path, monkeypatch) -> None:
    root = _proj(tmp_path / "old", "==2.7.*")
    monkeypatch.setattr(validator, "proc_run", lambda *a, **k: pytest.fail("built"))
    python, err = validator.ensure_test_venv(root, tmp_path / "cache")
    assert python is None and "requires-python" in err
