"""merge_poll: a report-only poll never consumes a change, and an odd dispatch result never
aborts the poll before its state is saved."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from auditor.merge_poll import poll_dev_tips


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_AUDIT_MERGE_POLL_ENABLED", "1")
    monkeypatch.setattr("auditor.merge_poll.time.sleep", lambda _s: None)
    mirrors = tmp_path / "mirrors.yaml"
    mirrors.write_text(
        "mirrors:\n"
        "  - name: argus-a\n    url: https://github.com/smk762/argus-a.git\n    path: /tmp/unused\n"
        "  - name: argus-b\n    url: https://github.com/smk762/argus-b.git\n    path: /tmp/unused2\n",
        encoding="utf-8",
    )
    tips = {"smk762/argus-a": "a" * 40, "smk762/argus-b": "b" * 40}
    with patch("auditor.merge_poll.fetch_branch_tip", side_effect=lambda slug, br, **kw: tips[slug]):
        yield {"mirrors": mirrors, "state": tmp_path / "state.json", "tips": tips}


def test_report_only_poll_does_not_consume_the_change(setup: dict[str, Any]) -> None:
    kw = {"mirrors_path": setup["mirrors"], "state_path": setup["state"]}
    poll_dev_tips(dispatch=lambda ev: {"accepted": True}, **kw)  # baseline
    setup["tips"]["smk762/argus-a"] = "c" * 40
    sent: list = []
    report = poll_dev_tips(enqueue=False, dispatch=lambda ev: sent.append(ev) or {"accepted": True}, **kw)
    assert report.changed == ["argus-a"] and sent == []

    later = poll_dev_tips(dispatch=lambda ev: sent.append(ev) or {"accepted": True}, **kw)
    assert later.changed == ["argus-a"]
    assert [(e.before, e.after) for e in sent] == [("a" * 40, "c" * 40)]


def test_baseline_only_still_advances_the_sha(setup: dict[str, Any]) -> None:
    kw = {"mirrors_path": setup["mirrors"], "state_path": setup["state"]}
    poll_dev_tips(baseline_only=True, enqueue=False, **kw)
    setup["tips"]["smk762/argus-a"] = "c" * 40
    poll_dev_tips(baseline_only=True, enqueue=False, **kw)
    state = json.loads(setup["state"].read_text(encoding="utf-8"))
    assert state["repos"]["argus-a"]["sha"] == "c" * 40


@pytest.mark.parametrize("returned", [None, True, False, "ok"])
def test_non_dict_dispatch_result_is_not_accepted_and_state_is_saved(
    setup: dict[str, Any], returned: Any
) -> None:
    kw = {"mirrors_path": setup["mirrors"], "state_path": setup["state"]}
    poll_dev_tips(dispatch=lambda ev: {"accepted": True}, **kw)  # baseline
    setup["tips"]["smk762/argus-a"] = "c" * 40
    setup["tips"]["smk762/argus-b"] = "d" * 40
    res = poll_dev_tips(dispatch=lambda ev: returned, **kw)
    assert res.changed == ["argus-a", "argus-b"]
    assert [q["accepted"] for q in res.queued] == [False, False]
    # Both repos were polled and the state saved, keeping the old SHAs for a retry.
    state = json.loads(setup["state"].read_text(encoding="utf-8"))
    assert state["repos"]["argus-a"]["sha"] == "a" * 40
    assert state["repos"]["argus-b"]["sha"] == "b" * 40
