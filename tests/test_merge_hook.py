"""Unit tests for merge-to-dev debounce scheduler + GitHub payload parsing."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from auditor.merge_hook import (
    MergeEvent,
    MergeHookScheduler,
    parse_github_push_event,
    verify_github_signature,
)


def test_verify_github_signature_roundtrip():
    secret = "whsec_test"
    body = b'{"ref":"refs/heads/dev"}'
    import hashlib
    import hmac

    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, sig, secret) is True
    assert verify_github_signature(body, "sha256=deadbeef", secret) is False
    assert verify_github_signature(body, None, "") is True  # secret unset → allow


def test_parse_github_push_event_filters_branch(monkeypatch):
    monkeypatch.setenv("AI_AUDIT_MERGE_BRANCH", "dev")
    ok = parse_github_push_event(
        {
            "ref": "refs/heads/dev",
            "before": "a" * 40,
            "after": "b" * 40,
            "repository": {"name": "argus-cortex"},
        }
    )
    assert ok is not None
    assert ok.repo == "argus-cortex"
    assert ok.before.startswith("a")
    assert ok.after.startswith("b")

    ignored = parse_github_push_event(
        {
            "ref": "refs/heads/main",
            "repository": {"name": "argus-cortex"},
        }
    )
    assert ignored is None


def test_coalesce_keeps_earliest_before_and_latest_after():
    first = MergeEvent(repo="r", before="111", after="222", received_at=1.0)
    second = MergeEvent(repo="r", before="222", after="333", received_at=2.0)
    merged = first.coalesce(second)
    assert merged.before == "111"
    assert merged.after == "333"
    assert merged.received_at == 1.0


def test_scheduler_debounces_and_coalesces(monkeypatch):
    monkeypatch.setenv("AI_AUDIT_MERGE_HOOK_ENABLED", "1")
    monkeypatch.setenv("AI_AUDIT_MERGE_DEBOUNCE_S", "1")
    monkeypatch.setenv("AI_AUDIT_MERGE_GLOBAL_GAP_S", "0")

    sched = MergeHookScheduler()
    calls: list[MergeEvent] = []

    def fake_run(event, **kwargs):
        calls.append(event)
        return {"repo": event.repo, "ok": True, "agent_packs": [], "findings": 0}

    with patch("auditor.merge_hook.run_merge_refresh", side_effect=fake_run):
        r1 = sched.enqueue(MergeEvent(repo="argus-a", before="aaa", after="bbb"))
        assert r1["accepted"] is True
        assert r1["debounced_s"] == 1
        r2 = sched.enqueue(MergeEvent(repo="argus-a", before="bbb", after="ccc"))
        assert r2["coalesced"] is True

        deadline = time.time() + 5
        while time.time() < deadline and len(calls) < 1:
            time.sleep(0.05)

    assert len(calls) == 1
    assert calls[0].before == "aaa"
    assert calls[0].after == "ccc"


def test_scheduler_serialises_repos_with_global_gap(monkeypatch):
    monkeypatch.setenv("AI_AUDIT_MERGE_HOOK_ENABLED", "1")
    monkeypatch.setenv("AI_AUDIT_MERGE_DEBOUNCE_S", "0")
    monkeypatch.setenv("AI_AUDIT_MERGE_GLOBAL_GAP_S", "1")

    sched = MergeHookScheduler()
    order: list[str] = []
    stamps: list[float] = []

    def fake_run(event, **kwargs):
        order.append(event.repo)
        stamps.append(time.time())
        return {"repo": event.repo, "ok": True}

    with patch("auditor.merge_hook.run_merge_refresh", side_effect=fake_run):
        sched.enqueue(MergeEvent(repo="repo-one", force=True))
        sched.enqueue(MergeEvent(repo="repo-two", force=True))
        deadline = time.time() + 6
        while time.time() < deadline and len(order) < 2:
            time.sleep(0.05)

    assert order == ["repo-one", "repo-two"]
    assert stamps[1] - stamps[0] >= 0.9


def test_force_skips_debounce(monkeypatch):
    monkeypatch.setenv("AI_AUDIT_MERGE_DEBOUNCE_S", "60")
    monkeypatch.setenv("AI_AUDIT_MERGE_GLOBAL_GAP_S", "0")
    sched = MergeHookScheduler()
    seen: list[str] = []

    with patch(
        "auditor.merge_hook.run_merge_refresh",
        side_effect=lambda event, **kw: seen.append(event.repo) or {"repo": event.repo},
    ):
        sched.enqueue(MergeEvent(repo="fast", force=True))
        deadline = time.time() + 3
        while time.time() < deadline and not seen:
            time.sleep(0.05)
    assert seen == ["fast"]
