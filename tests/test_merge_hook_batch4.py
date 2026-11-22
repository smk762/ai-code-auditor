"""merge_hook: deletions are not merges, failed diffs are not clean, and the scheduler keeps
each job's status, timer and running flag to itself."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from auditor import merge_hook
from auditor.merge_hook import (
    MergeEvent,
    MergeHookScheduler,
    parse_github_push_event,
    run_merge_refresh,
)


def _push(**extra: Any) -> dict[str, Any]:
    return {"ref": "refs/heads/dev", "before": "a" * 40, "after": "b" * 40,
            "repository": {"name": "argus-cortex"}, **extra}


@pytest.mark.parametrize("payload", [
    _push(deleted=True, after=merge_hook._ZERO_SHA),
    _push(deleted=True),
    _push(after=merge_hook._ZERO_SHA),
    _push(after=""),
])
def test_branch_deletion_is_not_a_merge_event(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setenv("AI_AUDIT_MERGE_BRANCH", "dev")
    assert parse_github_push_event(payload) is None
    assert parse_github_push_event(_push()) is not None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def refresh_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    shas = []
    for i in range(2):
        (repo / "f.py").write_text(f"x = {i}\n", encoding="utf-8")
        _git(repo, "add", "f.py")
        _git(repo, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(repo, "rev-parse", "HEAD"))

    import auditor.agent_pack as ap
    import auditor.config as config
    import auditor.diff_auditor as da
    import auditor.repo_resolver as rr

    monkeypatch.setattr(config, "load_ecosystem_config",
                        lambda: SimpleNamespace(repos=[SimpleNamespace(name="r")], output_dir=str(tmp_path / "out")))
    monkeypatch.setattr(rr, "resolve_repo_path", lambda _cfg: repo)
    monkeypatch.setattr(ap, "pack_rag_ingest_enabled", lambda _x=None: False)
    monkeypatch.setattr(ap, "emit_world_model_packs", lambda **kw: [])
    state: dict[str, Any] = {"shas": shas, "diff_error": None}

    def fake_diff(_p, b, a="HEAD"):
        if state["diff_error"]:
            raise RuntimeError(state["diff_error"])
        return ""

    monkeypatch.setattr(da, "get_diff_between_refs", fake_diff)
    return state


def test_failed_diff_is_reported_not_recorded_as_clean(refresh_env: dict[str, Any]) -> None:
    c0, c1 = refresh_env["shas"]
    refresh_env["diff_error"] = "fatal: bad object " + c0
    out = run_merge_refresh(MergeEvent(repo="r", before=c0, after=c1), sync_mirror=False)
    assert out["ok"] is False
    assert "bad object" in out["error"]
    assert "empty diff" not in out["message"]


def test_an_empty_diff_is_still_ok(refresh_env: dict[str, Any]) -> None:
    c0, c1 = refresh_env["shas"]
    out = run_merge_refresh(MergeEvent(repo="r", before=c0, after=c1), sync_mirror=False)
    assert out["ok"] is True and out["error"] == ""
    assert out["message"].startswith("empty diff")


def test_run_ids_differ_within_the_same_second(refresh_env: dict[str, Any]) -> None:
    c0, c1 = refresh_env["shas"]
    with patch("auditor.merge_hook.time.time", return_value=1_700_000_000.0):
        a = run_merge_refresh(MergeEvent(repo="r", before=c0, after=c1), sync_mirror=False)
        b = run_merge_refresh(MergeEvent(repo="r", before=c0, after=c1), sync_mirror=False)
    assert a["run_id"] != b["run_id"]


def test_scheduler_marks_a_not_ok_refresh_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_MERGE_GLOBAL_GAP_S", "0")
    sched = MergeHookScheduler()
    with patch("auditor.merge_hook.run_merge_refresh",
               side_effect=lambda ev, **kw: {"repo": ev.repo, "ok": False, "error": "diff failed: x"}):
        sched.enqueue(MergeEvent(repo="r", force=True))
        assert sched.wait_idle(timeout=5, poll_s=0.01)
    assert sched._statuses["r"].state == "error"
    assert sched.status()["recent"][0]["ok"] is False


def test_finishing_a_job_leaves_a_newer_pending_status_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_MERGE_DEBOUNCE_S", "60")
    monkeypatch.setenv("AI_AUDIT_MERGE_GLOBAL_GAP_S", "0")
    sched = MergeHookScheduler()
    started, release = threading.Event(), threading.Event()

    def slow_run(event, **kw):
        started.set()
        release.wait(5)
        return {"repo": event.repo, "ok": True, "after": event.after}

    with patch("auditor.merge_hook.run_merge_refresh", side_effect=slow_run):
        sched.enqueue(MergeEvent(repo="x", before="a" * 40, after="b" * 40, force=True))
        assert started.wait(5)
        sched.enqueue(MergeEvent(repo="x", before="b" * 40, after="c" * 40))  # debounced, pending
        release.set()
        deadline = time.time() + 5
        while time.time() < deadline and not sched.status()["recent"]:
            time.sleep(0.01)
    try:
        st = sched._statuses["x"]
        assert st.state == "pending"
        assert st.after == "c" * 40
        assert st.result == {} and st.finished_at is None
        assert sched.status()["recent"][0]["after"] == "b" * 40
    finally:
        for t in list(sched._timers.values()):
            t.cancel()


def test_a_stale_debounce_callback_cannot_consume_the_new_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_MERGE_DEBOUNCE_S", "60")
    sched = MergeHookScheduler()
    try:
        sched.enqueue(MergeEvent(repo="x", after="a" * 40))
        old = sched._timers["x"]
        sched.enqueue(MergeEvent(repo="x", after="b" * 40))
        new = sched._timers["x"]
        assert new is not old
        # The old timer fired just before enqueue cancelled it and ran after enqueue released the lock.
        sched._on_debounce_fire(*old.args)
        assert sched._timers.get("x") is new
        assert "x" not in sched._ready
        assert sched._worker_thread is None
    finally:
        for t in list(sched._timers.values()):
            t.cancel()


def test_running_is_cleared_during_the_global_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_MERGE_GLOBAL_GAP_S", "2")
    sched = MergeHookScheduler()

    def fake_run(event, **kw):
        return {"repo": event.repo, "ok": True}

    with patch("auditor.merge_hook.run_merge_refresh", side_effect=fake_run):
        sched.enqueue(MergeEvent(repo="one", force=True))
        sched.enqueue(MergeEvent(repo="two", force=True))
        deadline = time.time() + 5
        while time.time() < deadline and not sched.status()["recent"]:
            time.sleep(0.01)
        time.sleep(0.3)  # the worker is now sleeping out the gap before "two"
        status = sched.status()
        assert status["recent"][0]["repo"] == "one"
        assert status["ready"] == ["two"]
        assert status["running"] is None
        assert sched.wait_idle(timeout=6, poll_s=0.05)


def test_a_failed_pre_sync_tip_capture_is_recorded_not_swallowed(
        refresh_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """pack-ai-code-auditor-64d7ea236b: without the pre-sync tip the range falls back to after~1,
    narrower than a multi-commit merge. The result has to say so instead of looking normal."""
    import auditor.repo_resolver as rr
    import conductor.mirrors as cm

    repo = Path(_git(tmp_path / "r", "rev-parse", "--show-toplevel"))
    calls = {"n": 0}

    def resolve(_cfg: Any) -> Path:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("mirror path unreadable")
        return repo

    monkeypatch.setattr(rr, "resolve_repo_path", resolve)
    monkeypatch.setattr(cm, "sync_one_mirror", lambda name, **kw: SimpleNamespace(
        ok=True, action="fast-forward", branch="dev", path=str(repo), error=""))
    out = run_merge_refresh(MergeEvent(repo="r", before="", after=""), sync_mirror=True)
    assert "mirror path unreadable" in out["sync"].get("pre_sync_tip_error", "")
