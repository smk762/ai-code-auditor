"""Regression tests for auditor/git_ops.py audit packs."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from auditor import git_ops
from auditor.git_ops import (
    GitError,
    _extract_filenames_from_patch,
    _git,
    _is_local_path_remote,
    apply_to_tempdir,
    commit_repair,
    create_repair_branch,
    get_current_branch,
    is_in_conflict_state,
    push_repair_branch,
)


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _init(repo: Path, *, commit: bool = True) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "t")
    _run(repo, "config", "commit.gpgsign", "false")
    if commit:
        (repo / "a.py").write_text("a = 1\n")
        (repo / "other.txt").write_text("o\n")
        (repo / ".env").write_text("SECRET=1\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "init")
    return repo


def _committed_files(repo: Path) -> set[str]:
    return set(_run(repo, "show", "--name-only", "--format=", "HEAD").split())


# ── patch filename parsing ───────────────────────────────────────────────────

def test_plain_diff_timestamps_are_not_part_of_the_filename() -> None:  # 261be56d65
    patch = (
        "--- src/app.py\t2024-01-01 12:00:00.000000000 +0000\n"
        "+++ src/app.py\t2024-01-02 12:00:00.000000000 +0000\n"
        "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        "--- a/gone.py\t2024-01-01 12:00:00 +0000\n"
        "+++ /dev/null\t1970-01-01 00:00:00 +0000\n"
        "@@ -1 +0,0 @@\n-y = 1\n"
    )
    assert _extract_filenames_from_patch(patch) == ["src/app.py", "gone.py"]


def test_c_quoted_paths_are_unquoted() -> None:  # b887ac9927
    patch = (
        'diff --git "a/dir/na\\303\\257ve file.py" "b/dir/na\\303\\257ve file.py"\n'
        '--- "a/dir/na\\303\\257ve file.py"\n'
        '+++ "b/dir/na\\303\\257ve file.py"\n'
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    assert _extract_filenames_from_patch(patch) == ["dir/naïve file.py"]


def test_hunk_lines_starting_with_plus_plus_are_not_headers() -> None:  # 45b42d65f2
    patch = (
        "--- a/README.md\n+++ b/README.md\n"
        "@@ -1,2 +1,3 @@\n"
        "--- x\n"
        "+++ fake header\n"
        "+++ /dev/null\n"
        " keep\n"
        "+tail\n"
    )
    assert _extract_filenames_from_patch(patch) == ["README.md"]


# ── _git wrapper ─────────────────────────────────────────────────────────────

def test_non_utf8_output_does_not_raise_unicode_error(tmp_path: Path) -> None:  # 5fa4530425
    repo = _init(tmp_path / "r")
    (repo / "latin1.txt").write_bytes(b"caf\xe9 \xff\xfe\n")
    _run(repo, "add", "latin1.txt")
    _run(repo, "commit", "-q", "-m", "latin1")
    out = _git(["show", "HEAD:latin1.txt"], repo).stdout
    assert out.startswith("caf")


def test_git_error_message_redacts_credentials(tmp_path: Path) -> None:  # 60ec153266
    repo = _init(tmp_path / "r")
    with pytest.raises(GitError) as ei:
        _git(["-c", "http.extraheader=Authorization: Bearer hdr-s3cret",
              "rev-parse", "--verify", "https://user:url-s3cret@example.invalid/x.git"], repo)
    msg = str(ei.value)
    assert "hdr-s3cret" not in msg and "url-s3cret" not in msg
    assert "rev-parse" in msg


# ── introspection ────────────────────────────────────────────────────────────

def test_unborn_branch_is_not_reported_as_head(tmp_path: Path) -> None:  # 311cc431c0
    repo = _init(tmp_path / "r", commit=False)
    assert get_current_branch(repo) == "main"


def test_conflict_state_seen_in_linked_worktree(tmp_path: Path) -> None:  # 3d27cee3bb
    repo = _init(tmp_path / "r")
    wt = tmp_path / "wt"
    _run(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert (wt / ".git").is_file()
    assert not is_in_conflict_state(wt)
    git_dir = Path(_run(wt, "rev-parse", "--absolute-git-dir").strip())
    (git_dir / "MERGE_HEAD").write_text(_run(repo, "rev-parse", "HEAD"))
    assert is_in_conflict_state(wt)


# ── branches ─────────────────────────────────────────────────────────────────

def test_base_branch_cannot_be_an_option(tmp_path: Path) -> None:  # 1d442b30ea
    repo = _init(tmp_path / "r")
    (repo / "a.py").write_text("a = 'local work'\n")
    for bad in ("-f", "--force", "--detach"):
        with pytest.raises(GitError):
            create_repair_branch(repo, "x", base_branch=bad)
    assert (repo / "a.py").read_text() == "a = 'local work'\n"
    assert get_current_branch(repo) == "main"


def test_branch_name_failure_restores_original_branch(tmp_path: Path, monkeypatch) -> None:  # 9ab59c1afc
    repo = _init(tmp_path / "r")
    _run(repo, "branch", "base")

    def boom(*_a, **_k):
        raise GitError("ref listing failed")

    monkeypatch.setattr(git_ops, "_pick_free_branch_name", boom)
    with pytest.raises(GitError):
        create_repair_branch(repo, "x", base_branch="base")
    assert get_current_branch(repo) == "main"


# ── committing ───────────────────────────────────────────────────────────────

def test_empty_message_does_not_raise_index_error(tmp_path: Path) -> None:  # 1c0619a798
    repo = _init(tmp_path / "r")
    res = commit_repair(repo, message="", paths=[".env"])
    assert res.sha == "" and res.short_message == ""
    res = commit_repair(repo, message="\n")
    assert res.sha == "" and res.files_staged == 0


def test_patch_files_outside_repo_are_refused(tmp_path: Path) -> None:  # 4b8b2d1ce4
    repo = _init(tmp_path / "r")
    patch = "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -0,0 +1 @@\n+x\n"
    with pytest.raises(GitError, match="outside repo"):
        commit_repair(repo, message="m", patch_text=patch)


def test_fallback_staging_skips_secret_files(tmp_path: Path) -> None:  # 627ce1cf21
    repo = _init(tmp_path / "r")
    (repo / "a.py").write_text("a = 2\n")
    (repo / ".env").write_text("SECRET=2\n")
    res = commit_repair(repo, message="fix")
    assert res.files_staged == 1
    assert _committed_files(repo) == {"a.py"}


def test_pre_staged_changes_are_not_swept_into_the_commit(tmp_path: Path) -> None:  # 764242bb05
    repo = _init(tmp_path / "r")
    (repo / "other.txt").write_text("user work\n")
    _run(repo, "add", "other.txt")
    (repo / "a.py").write_text("a = 3\n")
    res = commit_repair(repo, message="fix", paths=["a.py"])
    assert res.files_staged == 1
    assert _committed_files(repo) == {"a.py"}
    assert _run(repo, "diff", "--cached", "--name-only").split() == ["other.txt"]


# ── pushing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,local", [
    ("/srv/git/repo.git", True),
    ("../sibling", True),
    ("file:///srv/repo.git", True),
    ("git@backup/repo.git", True),
    ("https://github.com/o/r.git", False),
    ("HTTPS://github.com/o/r.git", False),
    ("git+ssh://host/r.git", False),
    ("ftp://host/r.git", False),
    ("deploy@host:org/repo.git", False),
    ("github.com:org/repo.git", False),
    ("myhost:repo", False),
    ("ext::ssh -i key host %S repo", False),
])
def test_remote_classification(url: str, local: bool) -> None:  # 31b553b18b
    assert _is_local_path_remote(url) is local


def _fake_push(monkeypatch, *, fail_with: str = "") -> list[list[str]]:
    calls: list[list[str]] = []
    real = git_ops._git

    def fake(args, cwd, **kw):
        if args and args[0] == "push":
            calls.append(list(args))
            if fail_with:
                raise GitError(fail_with)
            return subprocess.CompletedProcess(args, 0, "", "")
        return real(args, cwd, **kw)

    monkeypatch.setattr(git_ops, "_git", fake)
    return calls


def test_push_rejects_option_like_branch_and_remote(tmp_path: Path, monkeypatch) -> None:  # 8f4c56715c
    repo = _init(tmp_path / "r")
    _run(repo, "remote", "add", "origin", "https://example.invalid/o/r.git")
    calls = _fake_push(monkeypatch)
    for kw in ({"branch": "--force"}, {"branch": "--delete"}, {"remote": "--mirror"}):
        res = push_repair_branch(repo, **kw)
        assert not res.pushed and "invalid" in res.skipped_reason
    assert calls == []
    res = push_repair_branch(repo, branch="main")
    assert res.pushed
    assert calls == [["push", "--set-upstream", "origin", "refs/heads/main:refs/heads/main"]]


def test_push_never_exposes_remote_credentials(tmp_path: Path, monkeypatch, caplog) -> None:  # c1efa96c22
    repo = _init(tmp_path / "r")
    url = "https://x-access-token:ghp_s3cret@example.invalid/o/r.git"
    _run(repo, "remote", "add", "origin", url)
    caplog.set_level(logging.DEBUG, logger="auditor.git_ops")
    _fake_push(monkeypatch, fail_with=f"fatal: unable to access '{url}/': Could not resolve host")
    res = push_repair_branch(repo, branch="main")
    assert not res.pushed
    _fake_push(monkeypatch)
    ok = push_repair_branch(repo, branch="main")
    assert ok.pushed
    for text in (res.remote_url, res.skipped_reason, ok.remote_url, caplog.text):
        assert "ghp_s3cret" not in text
    assert ok.remote_url == "https://***@example.invalid/o/r.git"


# ── apply_to_tempdir ─────────────────────────────────────────────────────────

_ADD_PATCH = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"


def test_symlinks_to_host_paths_are_not_followed(tmp_path: Path) -> None:  # f59c87d3f5
    host = tmp_path / "host"
    host.mkdir()
    (host / "id_rsa").write_text("PRIVATE\n")
    repo = _init(tmp_path / "r")
    (repo / "leak").symlink_to(host)
    copy, cleanup = apply_to_tempdir(repo, _ADD_PATCH)
    try:
        assert (copy / "leak").is_symlink()
        assert (copy / "a.py").read_text() == "a = 2\n"
    finally:
        cleanup()


def test_dangling_symlink_does_not_fail_the_copy(tmp_path: Path) -> None:  # cc38414a9e
    repo = _init(tmp_path / "r")
    (repo / "broken").symlink_to(tmp_path / "does-not-exist")
    copy, cleanup = apply_to_tempdir(repo, _ADD_PATCH)
    try:
        assert (copy / "a.py").read_text() == "a = 2\n"
        assert (copy / "broken").is_symlink()
    finally:
        cleanup()
