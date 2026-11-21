"""Regression tests for auditor/validate_base.py audit packs (batch 4)."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from auditor import validate_base
from auditor.validate_base import BaseRef, export_tree, resolve_base

ENV_CFG = ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *ENV_CFG, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch) -> Path:
    """A checkout on dev with one commit and a tag, and an origin to fetch from."""
    monkeypatch.setenv("AI_AUDIT_VALIDATE_GIT_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("AI_AUDIT_VALIDATE_FETCH", raising=False)
    remote, repo = tmp_path / "remote.git", tmp_path / "mirror"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "dev", str(repo)], check=True)
    (repo / "a.py").write_text("A = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "a")
    _git(repo, "tag", "v1")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "dev", "--tags")
    _git(repo, "fetch", "-q", "origin")
    return repo


def test_plain_copy_never_reads_through_a_symlink(tmp_path: Path) -> None:  # 935d5da106
    secret = tmp_path / "host" / "id_rsa"
    secret.parent.mkdir()
    secret.write_text("PRIVATE KEY\n")
    plain = tmp_path / "plain"
    (plain / "pkg").mkdir(parents=True)
    (plain / "pkg" / "a.py").write_text("A = 1\n")
    (plain / "id_rsa").symlink_to(secret)
    (plain / "hostdir").symlink_to(secret.parent, target_is_directory=True)
    (plain / "loop").symlink_to(plain, target_is_directory=True)  # followed, this recurses
    (plain / "inner").symlink_to("pkg/a.py")  # stays inside the tree: kept as a link
    tree = tmp_path / "tree"
    export_tree(resolve_base(plain), plain, tree)
    assert (tree / "pkg" / "a.py").read_text() == "A = 1\n"
    for name in ("id_rsa", "hostdir", "loop"):
        assert not (tree / name).exists() and not (tree / name).is_symlink(), name
    assert not any("PRIVATE KEY" in p.read_text() for p in tree.rglob("*") if p.is_file())
    assert (tree / "inner").is_symlink() and (tree / "inner").read_text() == "A = 1\n"
    assert secret.read_text() == "PRIVATE KEY\n"


def test_failed_git_archive_reports_git_error_and_reaps_the_process(mirror: Path, tmp_path: Path,
                                                                   monkeypatch) -> None:  # 6968ea30cb
    procs: list[subprocess.Popen] = []
    real = subprocess.Popen

    def spy(*a, **k):
        p = real(*a, **k)
        procs.append(p)
        return p

    monkeypatch.setattr(validate_base.subprocess, "Popen", spy)
    git_dir = Path(_git(mirror, "rev-parse", "--absolute-git-dir"))
    bad = BaseRef(git_dir=git_dir, ref="origin/dev", sha="0" * 40)
    with pytest.raises(RuntimeError, match="git archive origin/dev failed"):
        export_tree(bad, mirror, tmp_path / "tree")
    assert procs and all(p.returncode is not None for p in procs)  # waited on, no zombie
    assert all(p.stdout is None or p.stdout.closed for p in procs)


def test_unwritable_lock_falls_back_to_the_mirror(mirror: Path, tmp_path: Path) -> None:  # a2d5cb82d2
    top = Path(_git(mirror, "rev-parse", "--show-toplevel"))
    key = f"{top.name}-{hashlib.sha1(str(top).encode()).hexdigest()[:8]}"
    (tmp_path / "cache" / f".{key}.lock").mkdir(parents=True)  # opening it for writing fails, even as root
    base = resolve_base(mirror)
    assert base.source == "mirror" and base.sha == _git(mirror, "rev-parse", "origin/dev")
    assert "unusable" in base.fetch_error


def test_slow_tag_copy_keeps_the_head(mirror: Path, tmp_path: Path, monkeypatch) -> None:  # 7522945aa5
    real = subprocess.run

    def slow_update_ref(cmd, *a, **k):
        if "update-ref" in cmd and "--stdin" in cmd:
            raise subprocess.TimeoutExpired(cmd, 30)
        return real(cmd, *a, **k)

    monkeypatch.setattr(validate_base.subprocess, "run", slow_update_ref)
    base = resolve_base(mirror, fetch=False)
    tree = tmp_path / "tree"
    export_tree(base, mirror, tree)
    assert (tree / ".git").is_dir()  # tags are best effort; the HEAD is kept
    assert _git(tree, "rev-parse", "HEAD") == base.sha
