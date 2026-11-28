"""Pack GC: git failures read as unknown, literal paths, and the architecture hold's denominator."""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from auditor import pack_gc
from auditor.pack_gc import Mirror


def _git(cwd: Path, *args: str, date: str = "") -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _commit(repo: Path, name: str, date: str) -> None:
    (repo / name).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name, date=date)


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(["git"], 60), FileNotFoundError("git")],
                         ids=["timeout", "no_git"])
def test_a_git_that_hangs_or_is_missing_reads_as_unknown(tmp_path: Path, monkeypatch, exc) -> None:
    repo = tmp_path / "m"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "a.py", "2026-11-01T00:00:00+00:00")
    m = Mirror(repo)
    assert m.ok

    def run(*a, **kw):
        raise exc

    monkeypatch.setattr(subprocess, "run", run)
    assert m.lines("a.py") is None
    assert m.last_change("a.py") is None
    assert m.files() is None  # unknown: never "every file is gone"


def test_a_listing_that_fails_never_closes_a_pack_as_file_gone(tmp_path: Path) -> None:
    repo = tmp_path / "m"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "a.py", "2026-11-01T00:00:00+00:00")
    m = Mirror(repo)
    m.ref = "refs/heads/no-such-branch"  # ls-tree fails; the mirror still looks usable
    assert m.files() is None
    from auditor.trust_metrics import PackView

    p = PackView(id="p1", kind="nightly", repo="r", path="a.py", line=1, title="t", status="open",
                 detected_at=None, run_id="")
    assert pack_gc._file_gone(p, m.files()) is False


def test_last_change_reads_a_glob_looking_path_literally(tmp_path: Path) -> None:
    repo = tmp_path / "m"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "a[b].py", "2026-10-01T00:00:00+00:00")
    _commit(repo, "ab.py", "2026-11-01T00:00:00+00:00")  # matched by the glob a[b].py
    m = Mirror(repo)
    assert m.last_change("a[b].py") == datetime(2026, 10, 1, tzinfo=UTC)


def _arch(i: int, repo: str) -> dict:
    return {"id": f"pack-arch-{repo}-{i}", "kind": "architecture", "status": "open", "repo": repo,
            "title": f"rule in {repo} m{i}.py", "file_path": f"m{i}.py"}


def test_the_hold_counts_only_packs_that_could_close_as_not_reported() -> None:
    entries = [_arch(i, "unread") for i in range(20)] + [_arch(i, "read") for i in range(12)]
    p = pack_gc.plan_architecture(entries, [], {"unread", "read"}, unread_repos={"unread"})
    assert len(p["unknown"]) == 20
    assert "rerun with force" in p["held"] and "of 12 " in p["held"]
    assert p["closures"] == []
    forced = pack_gc.plan_architecture(entries, [], {"unread", "read"}, unread_repos={"unread"}, force=True)
    assert len(forced["closures"]) == 12
