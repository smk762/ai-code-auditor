"""conductor.code_review: verdict from the reviewer's JSON; a failed review runs no fix rounds."""

from __future__ import annotations

import json
from typing import Any

import pytest

import conductor.code_review as cr
from conductor.missions import WorkerResult
from conductor.schemas import DraftPr


def _draft() -> DraftPr:
    return DraftPr(url="https://example.invalid/pr/1", title="t", severity="HIGH", repo="r")


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(cr, "load_review_config", lambda: cr.ReviewConfig(
        intensity_default="high", intensity_by_mission={}, max_fix_rounds={"high": 2},
    ))

    def _run(review: WorkerResult) -> tuple[DraftPr, list[str]]:
        missions: list[str] = []

        def worker(task: Any, dry_run: bool = False) -> WorkerResult:
            missions.append(task.mission)
            if task.mission == "pr_fix":
                return WorkerResult(task_id=task.id, status="COMPLETED", summary="fixed")
            return review

        monkeypatch.setattr(cr, "run_worker", worker)
        return cr.review_draft_pr(_draft(), mission="security", repo_path="/nonexistent"), missions

    return _run


def test_escalate_in_prose_is_not_an_escalation(run: Any) -> None:
    raw = json.dumps({
        "status": "COMPLETED", "verdict": "needs_fix",
        "summary": "No need to escalate; one privilege-escalation path to fix.",
        "findings": [{"file": "a.py", "title": "x", "severity": "HIGH"}],
    })
    review = WorkerResult(task_id="t", status="COMPLETED", summary="", raw=raw,
                          findings=json.loads(raw)["findings"])
    draft, missions = run(review)
    assert missions.count("pr_fix") == 2  # the fix rounds the intensity allows were used
    assert draft.code_review is not None and draft.code_review.fix_rounds == 2


def test_failed_review_runs_no_fix_rounds(run: Any) -> None:
    review = WorkerResult(task_id="t", status="FAILED", summary="worker timed out", raw="")
    draft, missions = run(review)
    assert "pr_fix" not in missions
    assert draft.code_review is not None
    assert draft.code_review.status == "review_failed"
    assert draft.code_review.fix_rounds == 0
