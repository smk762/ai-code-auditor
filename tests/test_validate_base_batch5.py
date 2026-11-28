"""Regression tests for auditor/validate_base.py audit packs (batch 5)."""
from __future__ import annotations

import errno
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from auditor import validate_base
from auditor.validate_base import export_tree, resolve_base

ENV_CFG = ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *ENV_CFG, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch) -> Path:
    """A checkout on dev with one commit, and an origin (dev and main) to fetch from."""
    monkeypatch.setenv("AI_AUDIT_VALIDATE_GIT_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("AI_AUDIT_VALIDATE_FETCH", raising=False)
    monkeypatch.delenv("AI_AUDIT_VALIDATE_FETCH_TTL_S", raising=False)
    remote, repo = tmp_path / "remote.git", tmp_path / "mirror"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "dev", str(repo)], check=True)
    (repo / "a.py").write_text("A = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "a")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "dev", "dev:main")
    _git(repo, "fetch", "-q", "origin")
    return repo


def _commit_and_push(repo: Path, name: str, text: str, refspec: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)
    _git(repo, "push", "-q", "origin", refspec)
    return _git(repo, "rev-parse", "HEAD")


def test_git_dir_with_cwd_uses_cwd_as_the_work_tree(mirror: Path, tmp_path: Path, monkeypatch) -> None:  # 778f58af1d
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the auditor's own directory, which must not stand in for the work tree
    r = validate_base._git("status", "--porcelain", cwd=mirror, git_dir=mirror / ".git")
    assert r.returncode == 0
    assert r.stdout.strip() == ""  # read against elsewhere/, a.py shows as deleted


def test_an_escaping_link_is_left_out_of_a_git_export_like_a_copy(mirror: Path, tmp_path: Path) -> None:  # 92d3b21d02
    (mirror / "abs").symlink_to("/etc/hostname")
    (mirror / "up").symlink_to("../../outside")
    (mirror / "inner").symlink_to("a.py")
    _git(mirror, "add", "abs", "up", "inner")
    _git(mirror, "commit", "-qm", "links")
    _git(mirror, "push", "-q", "origin", "dev")
    base = resolve_base(mirror)
    assert base.git_dir is not None
    tree = tmp_path / "tree"
    export_tree(base, mirror, tree)
    assert (tree / "a.py").read_text() == "A = 1\n"
    for name in ("abs", "up"):
        assert not (tree / name).exists() and not (tree / name).is_symlink(), name
    assert (tree / "inner").is_symlink() and (tree / "inner").read_text() == "A = 1\n"


def test_an_extraction_error_is_reported_as_itself_not_as_git(mirror: Path, tmp_path: Path,
                                                               monkeypatch) -> None:  # 0c884ce637
    # big enough that git is still writing (the pipe is full) when the extraction fails
    (mirror / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    _git(mirror, "add", "big.bin")
    _git(mirror, "commit", "-qm", "big")
    _git(mirror, "push", "-q", "origin", "dev")
    base = resolve_base(mirror)

    def full_disk(self, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tarfile.TarFile, "extractall", full_disk)
    with pytest.raises(OSError, match="No space left on device"):
        export_tree(base, mirror, tmp_path / "tree")


def test_a_fetch_of_one_branch_does_not_make_another_fresh(mirror: Path, tmp_path: Path) -> None:  # 47e084c545
    first_main = resolve_base(mirror, branch="main")
    assert first_main.source == "fetched"
    for stamp in (tmp_path / "cache").glob(".*.fetched"):  # main was fetched days ago
        os.utime(stamp, (0, 0))
    new_main = _commit_and_push(mirror, "m.txt", "m\n", "HEAD:main")
    assert resolve_base(mirror, branch="dev").source == "fetched"
    again = resolve_base(mirror, branch="main")  # within the TTL of dev's fetch, not of main's
    assert again.sha == new_main
