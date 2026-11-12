"""Unit tests for GitHub slug parsing + poll state / change detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from auditor.merge_poll import parse_github_slug, poll_dev_tips


def test_parse_github_slug_variants():
    assert parse_github_slug("https://github.com/smk762/argus-cortex.git") == "smk762/argus-cortex"
    assert parse_github_slug("git@github.com:smk762/agent-composer.git") == "smk762/agent-composer"
    assert parse_github_slug("https://github.com/smk762/voluptas") == "smk762/voluptas"
    assert parse_github_slug("https://gitlab.com/x/y.git") is None


def test_poll_baselines_then_queues_on_change(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_AUDIT_MERGE_POLL_ENABLED", "1")
    monkeypatch.setenv("AI_AUDIT_MERGE_HOOK_ENABLED", "1")
    state = tmp_path / "state.json"
    mirrors = tmp_path / "mirrors.yaml"
    mirrors.write_text(
        "mirrors:\n"
        "  - name: argus-cortex\n"
        "    url: https://github.com/smk762/argus-cortex.git\n"
        "    path: /tmp/unused\n"
        "  - name: argus-lens\n"
        "    url: https://github.com/smk762/argus-lens.git\n"
        "    path: /tmp/unused2\n",
        encoding="utf-8",
    )

    tips = {"smk762/argus-cortex": "aaa", "smk762/argus-lens": "lll"}

    def fake_tip(slug: str, branch: str, **kwargs) -> str:
        return tips[slug]

    with patch("auditor.merge_poll.fetch_branch_tip", side_effect=fake_tip):
        first = poll_dev_tips(
            mirrors_path=mirrors,
            state_path=state,
            enqueue=True,
            force_queue=True,
        )
    assert sorted(first.baselined) == ["argus-cortex", "argus-lens"]
    assert first.changed == []
    assert first.queued == []

    tips["smk762/argus-cortex"] = "bbb"
    enqueued: list = []

    class FakeSched:
        def enqueue(self, event):
            enqueued.append(event)
            return {"accepted": True, "repo": event.repo, "before": event.before, "after": event.after}

    with (
        patch("auditor.merge_poll.fetch_branch_tip", side_effect=fake_tip),
        patch("auditor.merge_hook.get_scheduler", return_value=FakeSched()),
    ):
        second = poll_dev_tips(
            mirrors_path=mirrors,
            state_path=state,
            enqueue=True,
            force_queue=True,
        )

    assert second.changed == ["argus-cortex"]
    assert "argus-lens" in second.unchanged
    assert second.changes == [{"repo": "argus-cortex", "before": "aaa", "after": "bbb"}]
    assert len(enqueued) == 1
    assert enqueued[0].before == "aaa"
    assert enqueued[0].after == "bbb"
    assert second.queued[0]["accepted"] is True


def test_parse_github_slug_dotted_repo_name():
    assert parse_github_slug("git@github.com:owner/foo.js.git") == "owner/foo.js"
    assert parse_github_slug("https://github.com/owner/foo.js") == "owner/foo.js"


def test_poll_keeps_previous_sha_when_dispatch_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_AUDIT_MERGE_POLL_ENABLED", "1")
    state = tmp_path / "state.json"
    mirrors = tmp_path / "mirrors.yaml"
    mirrors.write_text(
        "mirrors:\n"
        "  - name: argus-cortex\n"
        "    url: https://github.com/smk762/argus-cortex.git\n"
        "    path: /tmp/unused\n",
        encoding="utf-8",
    )
    tips = {"smk762/argus-cortex": "aaa"}

    def fake_tip(slug: str, branch: str, **kwargs) -> str:
        return tips[slug]

    def failing_dispatch(event):
        raise OSError("connection refused")

    with patch("auditor.merge_poll.fetch_branch_tip", side_effect=fake_tip):
        poll_dev_tips(mirrors_path=mirrors, state_path=state, dispatch=failing_dispatch)
        tips["smk762/argus-cortex"] = "bbb"
        failed = poll_dev_tips(mirrors_path=mirrors, state_path=state, dispatch=failing_dispatch)
        assert failed.changed == ["argus-cortex"]
        assert failed.errors and failed.queued == []

        sent: list = []
        retried = poll_dev_tips(
            mirrors_path=mirrors,
            state_path=state,
            dispatch=lambda ev: sent.append(ev) or {"accepted": True},
        )
    assert retried.changed == ["argus-cortex"]
    assert sent[0].before == "aaa" and sent[0].after == "bbb"
