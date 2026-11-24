"""The structure scan parses on its own, newer interpreter (AI_AUDIT_SCAN_PYTHON, auditor/structure.py)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from auditor import structure
from auditor.validator import TEST_PYTHONS

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {"app/a.py": "from app import b\n", "app/b.py": "from app import a\n"}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for path, text in SOURCES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo)]
    for args in (["init", "-q", "-b", "dev"], ["add", "."], ["commit", "-qm", "x"],
                 ["update-ref", "refs/remotes/origin/dev", "HEAD"]):
        subprocess.run([*git, *args], check=True)
    return repo


def test_every_scan_runs_on_the_named_interpreter(tmp_path: Path, monkeypatch) -> None:
    repo, log = _repo(tmp_path), tmp_path / "log"
    wrapper = tmp_path / "scan-python"
    wrapper.write_text(f'#!/bin/sh\necho used >> "{log}"\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("AI_AUDIT_SCAN_PYTHON", str(wrapper))
    mirror, tree = structure.scan_mirror(repo), structure.scan_tree_isolated(repo)
    assert log.read_text().splitlines() == ["used", "used"]
    assert mirror["complete"] and tree["complete"] and mirror["cycles"] == tree["cycles"] != []


@pytest.mark.parametrize("python", ["/nonexistent/python", "/bin/false"])
def test_an_unusable_interpreter_is_an_error_never_a_fallback(tmp_path: Path, monkeypatch, python: str) -> None:
    monkeypatch.setenv("AI_AUDIT_SCAN_PYTHON", python)
    for scan in (structure.scan_mirror(_repo(tmp_path)), structure.scan_tree_isolated(tmp_path)):
        assert scan["error"] and scan["complete"] is False and "cycles" not in scan


def test_the_child_needs_no_installed_packages() -> None:
    p = subprocess.run([sys.executable, "-S", "-m", "auditor.structure", "-"], cwd=ROOT, input=json.dumps(SOURCES),
                       capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin"}, check=False)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == json.loads(json.dumps(structure.scan_sources(SOURCES)))


def test_odd_paths_and_a_large_repo_survive_the_pipe() -> None:
    sources = {**SOURCES, "app/\udcff.py": "x = 1\n", "app/é.py": "y = 'é'\n",
               **{f"app/m{i}.py": "x = 1  # " + "p" * 4000 + "\n" for i in range(100)}}
    assert structure.scan_sources_isolated(sources) == json.loads(json.dumps(structure.scan_sources(sources)))


def test_the_image_parses_every_python_it_can_test() -> None:
    text = (ROOT / "Dockerfile").read_text()
    baked = re.search(r"^ARG SCAN_PYTHON_VERSION=(\S+)", text, re.M)[1]
    assert "ENV AI_AUDIT_SCAN_PYTHON=" in text
    def key(v: str) -> list[int]:
        return [int(x) for x in v.split(".")]
    assert key(baked) >= max(map(key, TEST_PYTHONS))
