"""Mirror sync: fast-forward, reset after an upstream rewrite, never lose unique local work."""
from __future__ import annotations

import subprocess
from pathlib import Path

from conductor.mirrors import _sync_mirror_item, checkout_scan_branch

ENV_CFG = ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *ENV_CFG, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(cwd: Path, name: str, text: str, msg: str) -> str:
    (cwd / name).write_text(text)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-qm", msg)
    return _git(cwd, "rev-parse", "HEAD")


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """origin (bare) with dev: base → a → b; a mirror clone at b; a second clone to rewrite upstream."""
    remote, mirror, other = tmp_path / "remote.git", tmp_path / "mirror", tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
    _git(other, "checkout", "-q", "-b", "dev")
    _commit(other, "base.txt", "base\n", "base")
    _commit(other, "a.txt", "a\n", "a")
    _commit(other, "b.txt", "b\n", "b")
    _git(other, "push", "-q", "origin", "dev")
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(remote), str(mirror)], check=True, capture_output=True)
    return mirror, other


def _rewrite_upstream(other: Path) -> None:
    """Squash a+b into one commit and add a new one: same work, new hashes."""
    _git(other, "reset", "-q", "--soft", "HEAD~2")
    _git(other, "commit", "-qm", "a+b squashed")
    _commit(other, "c.txt", "c\n", "c")
    _git(other, "push", "-q", "-f", "origin", "dev")


def test_rewritten_upstream_resets_and_keeps_backup_and_stash(tmp_path: Path) -> None:
    mirror, other = _setup(tmp_path)
    old = _git(mirror, "rev-parse", "--short=12", "HEAD")
    (mirror / "base.txt").write_text("local edit\n")  # uncommitted, like a generated AGENTS.md section
    _rewrite_upstream(other)

    res = _sync_mirror_item({"name": "m", "path": str(mirror)}, preferred="dev")
    assert res.ok and res.action == "fetch+reset" and res.error == ""
    assert _git(mirror, "rev-parse", "HEAD") == _git(mirror, "rev-parse", "origin/dev")
    assert (mirror / "c.txt").exists()
    assert _git(mirror, "rev-parse", "--short=12", f"backup/pre-sync-{old}") == old
    assert "before reset of dev" in _git(mirror, "stash", "list")
    _git(mirror, "stash", "pop", "-q")
    assert (mirror / "base.txt").read_text() == "local edit\n"


def test_unique_local_commit_is_never_reset(tmp_path: Path) -> None:
    mirror, other = _setup(tmp_path)
    mine = _commit(mirror, "mine.txt", "mine\n", "unpushed work")
    _rewrite_upstream(other)

    rep: dict[str, str] = {}
    assert checkout_scan_branch(mirror, "dev", report=rep) == "dev"
    assert rep == {"update": "diverged"}
    assert _git(mirror, "rev-parse", "HEAD") == mine
    assert "backup/" not in _git(mirror, "branch", "--list")
    res = _sync_mirror_item({"name": "m", "path": str(mirror)}, preferred="dev")
    assert res.ok and res.action == "fetch+diverged" and "left as-is" in res.error


def test_plain_fast_forward(tmp_path: Path) -> None:
    mirror, other = _setup(tmp_path)
    _commit(other, "c.txt", "c\n", "c")
    _git(other, "push", "-q", "origin", "dev")
    res = _sync_mirror_item({"name": "m", "path": str(mirror)}, preferred="dev")
    assert res.action == "fetch+ff" and (mirror / "c.txt").exists()


def test_failed_checkout_never_resets_the_current_branch(tmp_path: Path) -> None:
    mirror, other = _setup(tmp_path)
    _git(mirror, "checkout", "-q", "-b", "work", "HEAD~1")
    work = _commit(mirror, "b.txt", "different b\n", "work b")
    (mirror / "b.txt").write_text("dirty\n")  # blocks `git checkout dev`
    _rewrite_upstream(other)

    rep: dict[str, str] = {}
    checkout_scan_branch(mirror, "dev", report=rep)
    assert rep == {"update": "checkout-failed"}
    assert _git(mirror, "rev-parse", "HEAD") == work and (mirror / "b.txt").read_text() == "dirty\n"


def test_rebased_upstream_is_matched_commit_by_commit(tmp_path: Path) -> None:
    """The argus-atlas case: upstream replayed the same commits with new hashes."""
    mirror, other = _setup(tmp_path)
    _git(other, "checkout", "-q", "--orphan", "new")
    _git(other, "rm", "-rqf", ".")
    _commit(other, "base.txt", "base\n", "base (new root)")
    _git(other, "cherry-pick", "dev~1", "dev")
    _git(other, "push", "-q", "-f", "origin", "new:dev")
    res = _sync_mirror_item({"name": "m", "path": str(mirror)}, preferred="dev")
    assert res.action == "fetch+reset"
    assert _git(mirror, "rev-parse", "HEAD") == _git(mirror, "rev-parse", "origin/dev")
