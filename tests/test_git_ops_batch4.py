"""Regression tests for auditor/git_ops.py audit packs (batch 4)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from auditor import git_ops
from auditor.git_ops import (
    GitError,
    _c_unquote,
    _extract_filenames_from_patch,
    _valid_push_name,
    commit_repair,
    push_repair_branch,
)


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _init(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "t")
    _run(repo, "config", "commit.gpgsign", "false")
    (repo / "a.py").write_text("a = 1\n")
    (repo / "old.py").write_text("".join(f"line {n}\n" for n in range(20)))
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "init")
    return repo


def _fake_push(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = git_ops._git

    def fake(args, cwd, **kw):
        if args and args[0] == "push":
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        return real(args, cwd, **kw)

    monkeypatch.setattr(git_ops, "_git", fake)
    return calls


# ── e0cd20aefa: push names are plain names, not refspecs or URLs ─────────────

def test_push_names_reject_refspec_and_url_syntax() -> None:
    for bad in ("+main", ":main", "HEAD:refs/heads/p", "a..b", "x^y", "x~1", "a*b", "a?b",
                "a[b", "a\\b", "@{-1}", "-f", "", "a b"):
        assert not _valid_push_name(bad), bad
    for bad_remote in ("https://attacker.example/r.git", "file:///srv/r", "../other", "a/b", "h:r"):
        assert not _valid_push_name(bad_remote, remote=True), bad_remote
    assert _valid_push_name("repair/fix-1")
    assert _valid_push_name("origin", remote=True)


def test_push_refuses_force_refspec_and_unconfigured_remote(tmp_path: Path, monkeypatch) -> None:
    repo = _init(tmp_path / "r")
    _run(repo, "remote", "add", "origin", "https://example.invalid/o/r.git")
    calls = _fake_push(monkeypatch)
    res = push_repair_branch(repo, branch="+main")
    assert not res.pushed and "invalid" in res.skipped_reason
    res = push_repair_branch(repo, branch="main", remote="https://attacker.example/r.git")
    assert not res.pushed
    assert calls == []
    assert push_repair_branch(repo, branch="main").pushed


# ── a4768303a9: renames, copies and binary patches name every path ───────────

def test_rename_with_edits_names_both_paths() -> None:
    patch = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 90%\nrename from old.py\nrename to new.py\n"
        "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert set(_extract_filenames_from_patch(patch)) == {"old.py", "new.py"}


def test_pure_rename_copy_and_binary_blocks_are_not_dropped() -> None:
    patch = (
        "diff --git a/src/old.py b/src/new.py\n"
        "similarity index 100%\nrename from src/old.py\nrename to src/new.py\n"
        "diff --git a/base.py b/copied.py\n"
        "similarity index 100%\ncopy from base.py\ncopy to copied.py\n"
        "diff --git a/img/logo.png b/img/logo.png\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/img/logo.png and b/img/logo.png differ\n"
        "diff --git a/my file.bin b/my file.bin\n"
        "new file mode 100644\nindex 0000000..3333333\nGIT binary patch\nliteral 3\nKcmZ?wbN>Ja\n\n"
        "literal 0\nHcmV?d00001\n\n"
    )
    assert _extract_filenames_from_patch(patch) == [
        "src/old.py", "src/new.py", "copied.py", "img/logo.png", "my file.bin"]


def test_plain_diff_backup_name_is_not_staged() -> None:
    patch = "--- app.py.orig\n+++ app.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert _extract_filenames_from_patch(patch) == ["app.py"]


def test_commit_from_rename_patch_removes_the_old_path(tmp_path: Path) -> None:
    repo = _init(tmp_path / "r")
    _run(repo, "mv", "old.py", "new.py")
    patch = _run(repo, "diff", "--cached", "-M", "HEAD")
    _run(repo, "reset", "-q")  # the repair left the move in the work tree, unstaged
    res = commit_repair(repo, message="move", patch_text=patch)
    assert res.files_staged == 2
    assert _run(repo, "ls-tree", "--name-only", "HEAD").split() == ["a.py", "new.py"]
    assert _run(repo, "status", "--porcelain") == ""


# ── 53dd5887eb: a move commits its deletion too ──────────────────────────────

def test_rename_commits_the_deletion_of_the_old_path(tmp_path: Path) -> None:
    repo = _init(tmp_path / "r")
    _run(repo, "config", "diff.renames", "true")
    os.rename(repo / "old.py", repo / "new.py")
    res = commit_repair(repo, message="move", paths=["old.py", "new.py"])
    assert res.files_staged == 2
    assert _run(repo, "ls-tree", "--name-only", "HEAD").split() == ["a.py", "new.py"]
    assert _run(repo, "diff", "--cached", "--name-only") == ""


# ── 35373fe9e9: non-UTF-8 names survive decoding ─────────────────────────────

def test_c_unquote_keeps_non_utf8_bytes() -> None:
    name = _c_unquote('"caf\\351.py"')
    assert os.fsencode(name) == b"caf\xe9.py"
    assert _c_unquote('"caf\\351.py"') != _c_unquote('"caf\\352.py"')


def test_commit_of_a_latin1_named_file(tmp_path: Path) -> None:
    repo = _init(tmp_path / "r")
    raw = b"caf\xe9.py"
    (repo / os.fsdecode(raw)).write_bytes(b"x = 1\n")
    patch = '--- /dev/null\n+++ "b/caf\\351.py"\n@@ -0,0 +1 @@\n+x = 1\n'
    res = commit_repair(repo, message="latin1", patch_text=patch)
    assert res.files_staged == 1
    listed = subprocess.run(["git", "ls-tree", "-z", "--name-only", "HEAD"], cwd=repo,
                            check=True, capture_output=True).stdout.split(b"\0")
    assert raw in listed


# ── 3d9a671ef6: a failed commit leaves the user's staging as it was ──────────

def test_failed_commit_keeps_pre_existing_staging_on_the_same_path(tmp_path: Path) -> None:
    repo = _init(tmp_path / "r")
    (repo / "a.py").write_text("a = 2  # user staged\n")
    _run(repo, "add", "a.py")
    (repo / "a.py").write_text("a = 3  # repair\n")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    try:
        commit_repair(repo, message="fix", paths=["a.py"])
    except GitError:
        pass
    else:
        raise AssertionError("commit should fail")
    assert _run(repo, "show", ":a.py") == "a = 2  # user staged\n"
    assert not list((repo / ".git").glob("index-backup-*"))
