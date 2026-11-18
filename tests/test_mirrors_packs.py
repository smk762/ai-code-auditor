"""conductor.mirrors: an empty path never means the CWD; clone URLs can't be git options or odd transports."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import conductor.mirrors as cm


def test_empty_path_never_touches_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    touched: list[Any] = []
    monkeypatch.setattr(cm, "checkout_scan_branch", lambda *a, **kw: touched.append(a) or "dev")
    for item in ({"name": "m", "path": ""}, {"name": "m"}, {"name": "m", "path": "  "}):
        res = cm._sync_mirror_item(item, preferred="dev")
        assert not res.ok and res.action == "skip"
    assert touched == []


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 128, "", "nope")

    monkeypatch.setattr(cm.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize("url", [
    "--upload-pack=touch /tmp/pwned",
    "ext::sh -c touch% /tmp/pwned",
    "file:///etc",
    "/home/someone/private-repo",
])
def test_unsafe_clone_urls_are_refused(url: str, tmp_path: Path, git_calls: list[list[str]]) -> None:
    res = cm._sync_mirror_item({"name": "m", "path": str(tmp_path / "m"), "url": url}, preferred="dev")
    assert not res.ok
    assert not any("clone" in c for c in git_calls)


def test_https_clone_puts_url_after_end_of_options(tmp_path: Path, git_calls: list[list[str]]) -> None:
    url = "https://github.com/o/r.git"
    cm._sync_mirror_item({"name": "m", "path": str(tmp_path / "m"), "url": url}, preferred="dev")
    clones = [c for c in git_calls if "clone" in c]
    assert clones
    for c in clones:
        assert c.index("--") == c.index(url) - 1


# --- clone: no prompt, a timeout, and the real failure reported -----------------------------


def test_clone_never_prompts_and_has_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        seen.append(kw)
        if kw.get("timeout") is None:
            raise AssertionError("network git without a timeout would hang forever")
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(cm.subprocess, "run", fake_run)
    res = cm._sync_mirror_item({"name": "m", "path": str(tmp_path / "m"), "url": "https://example.invalid/r.git"},
                               preferred="dev")
    assert not res.ok and "timed out" in res.error
    assert seen and all(kw["env"]["GIT_TERMINAL_PROMPT"] == "0" for kw in seen)


def test_failed_fallback_clone_reports_its_own_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        if "--branch" in args:
            return subprocess.CompletedProcess(args, 128, "", "Remote branch dev not found")
        return subprocess.CompletedProcess(args, 128, "", "fatal: Authentication failed")

    monkeypatch.setattr(cm.subprocess, "run", fake_run)
    res = cm._sync_mirror_item({"name": "m", "path": str(tmp_path / "m"), "url": "https://example.invalid/r.git"},
                               preferred="dev")
    assert not res.ok and "Authentication failed" in res.error


# --- explicit branch wins over the config default ------------------------------------------


def test_explicit_branch_beats_config_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "mirrors.yaml"
    cfg.write_text(
        f"mirror_root: {tmp_path / 'root'}\ndefault_branch: dev\nmirrors:\n  - name: m\n    path: {tmp_path / 'm'}\n",
        encoding="utf-8",
    )
    used: list[str] = []
    monkeypatch.setattr(cm, "_sync_mirror_item", lambda item, preferred: used.append(preferred) or None)
    cm.sync_mirrors(cfg, branch="main")
    cm.sync_mirrors(cfg)
    assert used == ["main", "dev"]


# --- alias names stay inside the mirror directory ------------------------------------------


@pytest.mark.parametrize("alias", ["../escape", "/abs/elsewhere", "a/b", "..", "."])
def test_alias_outside_the_mirror_dir_is_refused(alias: str, tmp_path: Path) -> None:
    mirrors = tmp_path / "home" / "mirrors"
    (mirrors / "argus-cortex").mkdir(parents=True)
    victim_dir = tmp_path / "home" / "victim-target"
    victim_dir.mkdir()
    # A symlink an escaping alias could hit: it must be neither unlinked nor repointed.
    victim = tmp_path / "home" / "escape"
    victim.symlink_to(victim_dir, target_is_directory=True)
    cfg = {
        "mirror_root": str(mirrors),
        "aliases": {alias: "argus-cortex"},
        "mirrors": [{"name": "argus-cortex", "path": str(mirrors / "argus-cortex")}],
    }
    results = cm.ensure_checkout_aliases(cfg)
    assert results and all(not r.ok for r in results)
    assert victim.is_symlink() and victim.resolve() == victim_dir.resolve()
    assert sorted(p.name for p in mirrors.iterdir()) == ["argus-cortex"]


# --- a fast-forward that fails after stashing gives the changes back -----------------------


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_failed_retry_after_stash_restores_local_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote, mirror, other = tmp_path / "remote.git", tmp_path / "mirror", tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
    _g(other, "checkout", "-q", "-b", "dev")
    (other / "base.txt").write_text("base\n")
    _g(other, "add", "base.txt")
    _g(other, "commit", "-qm", "base")
    _g(other, "push", "-q", "origin", "dev")
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(remote), str(mirror)], check=True, capture_output=True)
    (other / "base.txt").write_text("upstream edit\n")
    _g(other, "commit", "-qam", "edit")
    _g(other, "push", "-q", "origin", "dev")
    (mirror / "base.txt").write_text("local edit\n")

    real_git = cm._git
    merges = {"n": 0}

    def flaky_git(args: list[str], cwd: Path, *a: Any, **kw: Any) -> Any:
        if args[:2] == ["merge", "--ff-only"]:
            merges["n"] += 1
            if merges["n"] >= 2:  # the retry after the stash fails too (e.g. a lock file)
                return subprocess.CompletedProcess(args, 128, "", "fatal: Unable to create index.lock")
        return real_git(args, cwd, *a, **kw)

    monkeypatch.setattr(cm, "_git", flaky_git)
    res = cm._sync_mirror_item({"name": "m", "path": str(mirror)}, preferred="dev")
    assert res.action == "fetch+diverged"
    assert (mirror / "base.txt").read_text() == "local edit\n"  # the local change is back
    assert _g(mirror, "stash", "list") == ""
