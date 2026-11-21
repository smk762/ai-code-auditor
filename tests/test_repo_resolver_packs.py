"""Regression tests for repo_resolver audit packs (cache clone safety, git argv hygiene)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from auditor import repo_resolver as rr
from auditor.config import RepoConfig


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _upstream(tmp_path: Path) -> Path:
    up = tmp_path / "upstream"
    up.mkdir()
    _git("init", "-q", "-b", "dev", cwd=up)
    _git("config", "user.email", "t@t", cwd=up)
    _git("config", "user.name", "T", cwd=up)
    (up / "a.py").write_text("x = 1\n")
    _git("add", ".", cwd=up)
    _git("commit", "-qm", "one", cwd=up)
    return up


def _repo(tmp_path: Path, up: Path, **kw) -> RepoConfig:
    return RepoConfig(name="r", path=up.as_uri(), local_cache_path=str(tmp_path / "cache"), **kw)


# --- branch_exists -----------------------------------------------------------------------------

def test_branch_exists_does_not_accept_revision_expressions(tmp_path: Path) -> None:
    up = _upstream(tmp_path)
    assert rr.branch_exists(up, "dev")
    for expr in ("dev~0", "dev^{commit}", "dev@{0}"):
        assert not rr.branch_exists(up, expr), expr


# --- diff_files --------------------------------------------------------------------------------

def test_diff_files_returns_unquoted_non_ascii_paths(tmp_path: Path) -> None:
    up = _upstream(tmp_path)
    _git("checkout", "-qb", "feat", cwd=up)
    (up / "café.py").write_text("y = 2\n")
    _git("add", ".", cwd=up)
    _git("commit", "-qm", "two", cwd=up)
    assert rr.diff_files(up, base="dev", head="feat") == frozenset({"café.py"})


def test_diff_files_refuses_option_like_ref(tmp_path: Path) -> None:
    up = _upstream(tmp_path)
    out = tmp_path / "written"
    assert rr.diff_files(up, base=f"--output={out}", head="dev") == frozenset()
    # Unpatched, git parses the whole token as --output=<out>...dev and writes that file.
    assert not list(tmp_path.glob("written*"))


def test_diff_files_timeout_is_an_empty_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(rr.subprocess, "run", hang)
    assert rr.diff_files(tmp_path, base="dev") == frozenset()


# --- remote cache ------------------------------------------------------------------------------

def test_cache_follows_a_force_pushed_upstream_and_drops_local_edits(tmp_path: Path) -> None:
    up = _upstream(tmp_path)
    repo = _repo(tmp_path, up)
    clone = rr._materialize_remote_repo(repo)
    assert (clone / "a.py").read_text() == "x = 1\n"
    # Rewrite upstream history and dirty the cache: pull --ff-only can never recover from this.
    (up / "a.py").write_text("x = 2\n")
    _git("add", ".", cwd=up)
    _git("commit", "-q", "--amend", "-m", "rewritten", cwd=up)
    (clone / "a.py").write_text("local edit\n")
    (clone / "junk.txt").write_text("untracked\n")
    rr._materialize_remote_repo(repo)
    assert (clone / "a.py").read_text() == "x = 2\n"
    assert not (clone / "junk.txt").exists()
    assert _git("rev-parse", "HEAD", cwd=clone) == _git("rev-parse", "dev", cwd=up)


def test_non_git_cache_dir_is_recloned_not_treated_as_a_clone(tmp_path: Path) -> None:
    # The cache sits inside another repository; a non-git cache dir must not let git act on it.
    outer = _upstream(tmp_path)
    up = tmp_path / "remote"
    up.mkdir()
    _git("init", "-q", "-b", "dev", cwd=up)
    _git("config", "user.email", "t@t", cwd=up)
    _git("config", "user.name", "T", cwd=up)
    (up / "remote.py").write_text("r = 1\n")
    _git("add", ".", cwd=up)
    _git("commit", "-qm", "r", cwd=up)
    repo = RepoConfig(name="r", path=up.as_uri(), local_cache_path=str(outer / ".cache" / "repos"))
    (outer / ".cache" / "repos" / "r").mkdir(parents=True)
    outer_head = _git("rev-parse", "HEAD", cwd=outer)
    clone = rr._materialize_remote_repo(repo)
    assert (clone / ".git").exists()
    assert (clone / "remote.py").exists()
    assert _git("rev-parse", "HEAD", cwd=outer) == outer_head
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=outer) == ""


@pytest.mark.parametrize("name", ["../outside", "/abs/path", "a/b", ".."])
def test_unsafe_repo_name_never_touches_a_dir_outside_the_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious")
    calls: list[list[str]] = []

    def fail(args, **k):
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", "fatal: destination exists")

    monkeypatch.setattr(rr.subprocess, "run", fail)
    repo = RepoConfig(name=name, path="https://example.invalid/r.git",
                      local_cache_path=str(tmp_path / "cache"))
    before = sorted(p.name for p in tmp_path.iterdir())

    def untouched() -> None:
        # Nothing outside the cache was removed or recreated: not the precious file, not its dir,
        # not the cache's parent (a name like ".." resolves there).
        assert (outside / "keep.txt").read_text() == "precious"
        assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]
        assert sorted(p.name for p in tmp_path.iterdir() if p.name != "cache") == \
            [n for n in before if n != "cache"]

    with pytest.raises((ValueError, RuntimeError)):
        rr._clone_with_branch_fallback(repo, None)
    untouched()
    with pytest.raises(ValueError):
        rr._materialize_remote_repo(repo)
    untouched()
    assert calls == []


def test_clone_puts_end_of_options_before_the_repo_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def ok(args, **k):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rr.subprocess, "run", ok)
    repo = RepoConfig(name="r", path="https://example.invalid/r.git", local_cache_path=str(tmp_path))
    rr._clone_with_branch_fallback(repo, None)
    argv = calls[0]
    assert argv[argv.index("--") + 1] == repo.path
    bad = RepoConfig(name="r", path="--upload-pack=touch pwned", local_cache_path=str(tmp_path))
    with pytest.raises(ValueError):
        rr._clone_with_branch_fallback(bad, None)
    assert len(calls) == 1


def test_clone_timeout_raises_runtime_error_and_never_prompts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    envs: list[dict] = []

    def hang(args, **k):
        envs.append(k.get("env") or {})
        assert "timeout" in k
        Path(args[-1]).mkdir(parents=True, exist_ok=True)  # a partial clone left behind
        raise subprocess.TimeoutExpired(cmd=args, timeout=k["timeout"])

    monkeypatch.setattr(rr.subprocess, "run", hang)
    repo = RepoConfig(name="r", path="https://example.invalid/r.git", local_cache_path=str(tmp_path))
    with pytest.raises(RuntimeError, match="timed out"):
        rr._clone_with_branch_fallback(repo, None)
    assert envs and all(e.get("GIT_TERMINAL_PROMPT") == "0" for e in envs)
    assert not (tmp_path / "r").exists()


# --- credentials -------------------------------------------------------------------------------

def test_git_errors_mask_url_credentials_and_the_token() -> None:
    err = ("fatal: unable to access 'https://x-access-token:ghs_SECRET123@github.com/o/r.git/': 403\n"
           "remote said ghs_SECRET123")
    result = subprocess.CompletedProcess(["git"], 128, "", err)
    with pytest.raises(RuntimeError) as exc:
        rr._ensure_git_ok(result, "r", "ghs_SECRET123")
    assert "ghs_SECRET123" not in str(exc.value)
    assert "https://***@github.com/o/r.git" in str(exc.value)
    with pytest.raises(RuntimeError) as exc:
        rr._ensure_git_ok(subprocess.CompletedProcess(["git"], 128, "", err.splitlines()[0]), "r")
    assert "SECRET" not in str(exc.value)


def test_askpass_script_is_reused_not_leaked_per_call() -> None:
    a = rr._git_env_with_token("tok-a")
    b = rr._git_env_with_token("tok-b")
    assert a is not None and b is not None
    assert a["GIT_ASKPASS"] == b["GIT_ASKPASS"]
    script = Path(a["GIT_ASKPASS"])
    assert script.is_file()
    assert "tok-a" not in script.read_text()
    assert b["GIT_TOKEN"] == "tok-b"
