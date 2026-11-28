"""auditor/structure.py: a git read that fails or comes back short is Unreadable; attribute uses
of a re-exported module or a public alias are not private-import findings."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from auditor import structure


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _mirror(tmp: Path) -> Path:
    repo = tmp / "demo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg/a.py").write_text("x = 1\n" * 50)
    (repo / "pkg/b.py").write_text("y = 2\n" * 50)
    _git(tmp, "init", "-q", "-b", "dev", str(repo))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "x")
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return repo


def _patch_cat_file(monkeypatch: pytest.MonkeyPatch, change) -> None:
    real = subprocess.run

    def run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "cat-file"]:
            return change(real(cmd, *a, **kw), cmd, kw)
        return real(cmd, *a, **kw)
    monkeypatch.setattr(structure.subprocess, "run", run)


@pytest.mark.parametrize("keep", [5, -10], ids=["in_header", "in_last_file"])
def test_batch_output_cut_short_is_unreadable_not_a_partial_file(tmp_path, monkeypatch, keep) -> None:
    repo = _mirror(tmp_path)
    assert structure.read_dev(repo)[1]["pkg/b.py"] == "y = 2\n" * 50

    def short(r, cmd, kw):
        r.stdout = r.stdout[:keep]
        return r
    _patch_cat_file(monkeypatch, short)
    with pytest.raises(structure.Unreadable):
        structure.read_dev(repo)


def test_a_git_timeout_is_unreadable(tmp_path, monkeypatch) -> None:
    repo = _mirror(tmp_path)

    def hang(r, cmd, kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
    _patch_cat_file(monkeypatch, hang)
    with pytest.raises(structure.Unreadable):
        structure.read_dev(repo)


def test_an_attribute_that_is_a_module_or_public_alias_is_not_a_private_import() -> None:
    src = {"pkg/__init__.py": "from other import _impl\nfrom other.pub import pub as _alias\nfrom other.own import _real\n",
           "other/__init__.py": "", "other/_impl.py": "x = 1\n", "other/pub.py": "def pub(): pass\n",
           "other/own.py": "def _real(): pass\n",
           "user.py": "import pkg\npkg._impl.x\npkg._alias()\npkg._real()\n"}
    found = structure.scan_sources(src)["private_imports"]
    assert [(p["defined_in"], p["name"]) for p in found if p["path"] == "user.py"] == [("other/own.py", "_real")]
