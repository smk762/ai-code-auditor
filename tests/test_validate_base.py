"""validate_patch's base: fetch dev on demand into a cache repo, never write the mirror."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from auditor import validate_base
from auditor.validate_base import BaseRefError, export_tree, pin_base, resolve_base, staleness

ENV_CFG = ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *ENV_CFG, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(cwd: Path, rel: str, text: str) -> str:
    (cwd / rel).parent.mkdir(parents=True, exist_ok=True)
    (cwd / rel).write_text(text)
    _git(cwd, "add", rel)
    _git(cwd, "commit", "-qm", rel)
    return _git(cwd, "rev-parse", "HEAD")


def _snapshot(repo: Path) -> dict[str, str]:
    """Every file under .git with its content hash and mtime: proves the mirror's repository isn't
    written, not even rewritten in place with the same bytes."""
    out = {}
    for p in sorted((repo / ".git").rglob("*")):
        if p.is_file():
            out[str(p.relative_to(repo))] = f"{hashlib.sha256(p.read_bytes()).hexdigest()} {p.stat().st_mtime_ns}"
    return out


def test_snapshot_sees_a_same_size_rewrite_past_the_header(tmp_path: Path) -> None:
    f = tmp_path / ".git" / "index"
    f.parent.mkdir()
    f.write_bytes(b"H" * 64 + b"a" * 64)
    before, mtime = _snapshot(tmp_path), f.stat().st_mtime_ns
    with open(f, "r+b") as fh:  # same length, same first 32 bytes
        fh.seek(100)
        fh.write(b"b")
    os.utime(f, ns=(mtime, mtime))  # even with the mtime put back, the content hash differs
    assert _snapshot(tmp_path) != before


@pytest.fixture
def repos(tmp_path: Path, monkeypatch):
    """A bare origin with dev, a mirror cloned at the first commit, and a pusher clone."""
    monkeypatch.setenv("AI_AUDIT_VALIDATE_GIT_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("AI_AUDIT_VALIDATE_FETCH", raising=False)
    monkeypatch.delenv("AI_AUDIT_VALIDATE_FETCH_TTL_S", raising=False)
    remote, mirror, pusher = tmp_path / "remote.git", tmp_path / "mirror", tmp_path / "pusher"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(pusher)], check=True, capture_output=True)
    _git(pusher, "checkout", "-q", "-b", "dev")
    _commit(pusher, "svc/app/old.py", "OLD = 1\n")
    _git(pusher, "push", "-q", "origin", "dev")
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(remote), str(mirror)], check=True, capture_output=True)
    return remote, mirror, pusher


def _merge_upstream(pusher: Path) -> str:
    sha = _commit(pusher, "svc/app/new.py", "NEW = 2\n")
    _git(pusher, "push", "-q", "origin", "dev")
    return sha


def test_fetches_code_merged_after_the_nightly_sync_without_touching_the_mirror(repos, tmp_path) -> None:
    _, mirror, pusher = repos
    tip = _merge_upstream(pusher)
    before = _snapshot(mirror)

    base = resolve_base(mirror / "svc")
    assert base.source == "fetched" and base.fresh and base.sha == tip and base.ref == "origin/dev"
    assert base.prefix == "svc/"
    export_tree(base, mirror / "svc", tmp_path / "tree")
    assert (tmp_path / "tree/app/new.py").read_text() == "NEW = 2\n"  # the subtree, at the top
    assert _snapshot(mirror) == before  # refs, objects, index: all untouched
    assert not (mirror / "svc/app/new.py").exists()


def test_ttl_skips_a_second_fetch(repos, monkeypatch) -> None:
    _, mirror, pusher = repos
    first = resolve_base(mirror)
    assert first.source == "fetched"
    newer = _merge_upstream(pusher)
    second = resolve_base(mirror)
    assert second.source == "cached" and second.sha == first.sha
    monkeypatch.setenv("AI_AUDIT_VALIDATE_FETCH_TTL_S", "0")
    third = resolve_base(mirror)
    assert third.source == "fetched" and third.sha == newer


def test_failed_fetch_falls_back_to_the_mirror_and_says_why(repos) -> None:
    _, mirror, _ = repos
    _git(mirror, "remote", "set-url", "origin", str(mirror.parent / "gone.git"))
    base = resolve_base(mirror)
    assert base.source == "mirror" and not base.fresh and base.ref == "origin/dev"
    assert base.sha == _git(mirror, "rev-parse", "origin/dev") and base.fetch_error


def test_fetch_timeout_is_bounded(repos, monkeypatch) -> None:
    _, mirror, _ = repos
    real = validate_base._git

    fetch_timeouts = []

    def slow(*args, **kw):
        if args and args[0] == "fetch":
            fetch_timeouts.append(kw.get("timeout"))
            raise subprocess.TimeoutExpired(cmd="git fetch", timeout=kw.get("timeout"))
        return real(*args, **kw)

    monkeypatch.setattr(validate_base, "_git", slow)
    base = resolve_base(mirror)
    assert base.source == "mirror" and "timed out" in base.fetch_error
    # the fetch itself was given a finite, positive bound (not left to the default or None)
    assert len(fetch_timeouts) == 1
    assert isinstance(fetch_timeouts[0], (int, float)) and 0 < fetch_timeouts[0] < float("inf")


@pytest.mark.parametrize("ttl,timeout", [("soon", "20s"), ("-1", "0"), ("nan", "inf")])
def test_malformed_fetch_env_falls_back_to_defaults(repos, monkeypatch, ttl, timeout) -> None:
    _, mirror, _ = repos
    monkeypatch.setenv("AI_AUDIT_VALIDATE_FETCH_TTL_S", ttl)
    monkeypatch.setenv("AI_AUDIT_VALIDATE_FETCH_TIMEOUT_S", timeout)
    real = validate_base._git
    seen = []
    monkeypatch.setattr(validate_base, "_git",
                        lambda *a, **kw: (seen.append(kw.get("timeout")) if a[0] == "fetch" else None) or real(*a, **kw))
    assert resolve_base(mirror).source == "fetched"
    assert seen == [20.0]


def test_relative_origin_url_is_resolved_against_the_mirror(repos, tmp_path, monkeypatch) -> None:
    _, mirror, pusher = repos
    tip = _merge_upstream(pusher)
    _git(mirror, "remote", "set-url", "origin", "../remote.git")  # relative to the mirror's top
    elsewhere = tmp_path / "a" / "b"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    base = resolve_base(mirror)
    assert base.source == "fetched" and base.sha == tip, base.fetch_error


@pytest.mark.parametrize("url", ["https://github.com/o/r.git", "git@github.com:o/r.git", "/srv/r.git",
                                 "file:///srv/r.git"])
def test_remote_urls_other_than_relative_paths_are_untouched(url) -> None:
    assert validate_base._fetchable_url(url, Path("/m/top")) == url


def test_fetch_disabled_uses_the_mirror(repos, monkeypatch) -> None:
    _, mirror, _ = repos
    monkeypatch.setenv("AI_AUDIT_VALIDATE_FETCH", "0")
    assert resolve_base(mirror).source == "mirror"


def test_not_a_checkout_is_a_copy(tmp_path) -> None:
    (tmp_path / "plain").mkdir()
    base = resolve_base(tmp_path / "plain")
    assert base.source == "copy" and base.git_dir is None


def test_staleness_against_the_merge_poll_tip(repos, tmp_path, monkeypatch) -> None:
    _, mirror, pusher = repos
    monkeypatch.setenv("AI_AUDIT_VALIDATE_FETCH", "0")
    state = tmp_path / "merge_poll_state.json"
    old = resolve_base(mirror)
    assert staleness(old, "m", state_path=state) == {"base_stale": None}  # merge-poll hasn't seen it

    tip = _merge_upstream(pusher)
    state.write_text(json.dumps({"repos": {"m": {"sha": tip}}}))
    assert staleness(old, "m", state_path=state) == {"base_stale": True, "dev_tip": tip}  # unknown locally

    _git(mirror, "fetch", "-q", "origin")
    assert staleness(old, "m", state_path=state) == {"base_stale": True, "dev_tip": tip, "behind_by": 1}
    monkeypatch.setenv("AI_AUDIT_VALIDATE_FETCH", "1")
    assert staleness(resolve_base(mirror), "m", state_path=state) == {"base_stale": False, "dev_tip": tip}


# ── a pinned base (#91) ──────────────────────────────────────────────────────

def test_pin_base_moves_back_to_an_ancestor_of_dev(repos, tmp_path) -> None:
    _, mirror, pusher = repos
    old = _git(pusher, "rev-parse", "HEAD")
    _merge_upstream(pusher)
    tip = resolve_base(mirror)
    pinned = pin_base(tip, old, mirror)
    assert pinned.sha == old and pinned.ref == old and pinned.source == "pinned" and not pinned.fresh
    assert pinned.git_dir == tip.git_dir
    export_tree(pinned, mirror, tmp_path / "tree")
    assert (tmp_path / "tree/svc/app/old.py").exists() and not (tmp_path / "tree/svc/app/new.py").exists()
    assert pin_base(tip, tip.sha, mirror).sha == tip.sha  # the tip itself is allowed


def test_pin_base_finds_a_commit_merged_after_the_nightly_sync_without_touching_the_mirror(repos) -> None:
    _, mirror, pusher = repos
    mid = _merge_upstream(pusher)  # the mirror never saw it; the dev fetch brings it into the cache
    _commit(pusher, "svc/app/later.py", "L = 3\n")
    _git(pusher, "push", "-q", "origin", "dev")
    before = _snapshot(mirror)
    assert pin_base(resolve_base(mirror), mid, mirror).sha == mid
    assert _snapshot(mirror) == before


@pytest.mark.parametrize("ref,why", [("--output=/tmp/x", "invalid git ref"), ("a..b", "invalid git ref"),
                                     ("nope", "not a commit"), ("f" * 40, "not a commit")])
def test_pin_base_refuses_unsafe_or_unknown_refs(repos, ref, why) -> None:
    _, mirror, _ = repos
    with pytest.raises(BaseRefError, match=why):
        pin_base(resolve_base(mirror), ref, mirror)


def test_pin_base_refuses_a_commit_off_dev(repos) -> None:
    _, mirror, pusher = repos
    _git(pusher, "checkout", "-q", "-b", "side")
    side = _commit(pusher, "svc/app/side.py", "S = 1\n")
    _git(pusher, "push", "-q", "origin", "side")
    _git(mirror, "fetch", "-q", "origin")  # the nightly sync knows every branch
    tip = resolve_base(mirror)
    for ref in (side, "origin/side"):
        with pytest.raises(BaseRefError, match="not an ancestor"):
            pin_base(tip, ref, mirror)


def test_pin_base_needs_a_git_mirror(tmp_path) -> None:
    (tmp_path / "plain").mkdir()
    with pytest.raises(BaseRefError, match="plain copy"):
        pin_base(resolve_base(tmp_path / "plain"), "dev", tmp_path / "plain")


# ── the exported tree has a git HEAD (#97) ─────────────────────────────────────

def test_export_has_head_at_the_base_and_its_history_without_touching_the_mirror(repos, tmp_path) -> None:
    _, mirror, pusher = repos
    first = _git(pusher, "rev-parse", "HEAD")
    _git(pusher, "tag", "-a", "v0.1.0", "-m", "v0.1.0")  # annotated, pushed after the mirror's sync
    tip = _merge_upstream(pusher)
    _git(pusher, "tag", "v9-elsewhere", first)
    _git(pusher, "checkout", "-q", "-b", "side")
    _git(pusher, "tag", "v-off-dev", _commit(pusher, "svc/app/side.py", "S = 1\n"))
    _git(pusher, "push", "-q", "origin", "--tags")
    before = _snapshot(mirror)
    base = resolve_base(mirror)
    tree = tmp_path / "tree"
    export_tree(base, mirror, tree)
    assert _git(tree, "rev-parse", "HEAD") == tip
    assert _git(tree, "rev-list", "HEAD").split() == [tip, first]  # history up to the base is reachable
    assert _git(tree, "status", "--porcelain") == ""
    assert _git(tree, "describe", "--tags", "--long", "--match", "v0*", "HEAD").startswith("v0.1.0-1-g")
    assert sorted(_git(tree, "tag").split()) == ["v0.1.0", "v9-elsewhere"]  # not the tag off dev
    (tree / "svc/app/old.py").write_text("OLD = 9\n")
    assert _git(tree, "diff", "--name-only") == "svc/app/old.py"  # a patch shows as a working-tree change
    assert _snapshot(mirror) == before


def test_subtree_export_head_sits_on_the_base(repos, tmp_path) -> None:
    _, mirror, pusher = repos
    tip = _merge_upstream(pusher)
    base = resolve_base(mirror / "svc")
    tree = tmp_path / "tree"
    export_tree(base, mirror / "svc", tree)
    assert _git(tree, "rev-parse", "HEAD^") == tip
    assert _git(tree, "status", "--porcelain") == "" and (tree / "app/new.py").exists()


def test_plain_copy_gets_no_repository(tmp_path) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain/a.py").write_text("A = 1\n")
    export_tree(resolve_base(tmp_path / "plain"), tmp_path / "plain", tmp_path / "tree")
    assert (tmp_path / "tree/a.py").exists() and not (tmp_path / "tree/.git").exists()
