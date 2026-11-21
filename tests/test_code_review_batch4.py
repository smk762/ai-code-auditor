"""conductor.code_review: the verdict survives quoted braces, is checked against the allowed
set, and only a completed review that didn't reject the PR is marked clean."""

from __future__ import annotations

import json
from typing import Any

import pytest

import conductor.code_review as cr
from conductor.missions import WorkerResult
from conductor.schemas import DraftPr


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
        draft = DraftPr(url="https://example.invalid/pr/1", title="t", severity="HIGH", repo="r")
        return cr.review_draft_pr(draft, mission="security", repo_path="/nonexistent"), missions

    return _run


def test_verdict_is_found_among_quoted_braces() -> None:
    raw = (
        "The handler builds `cfg = {\"retries\": 0}` and never checks it.\n"
        + json.dumps({"status": "COMPLETED", "verdict": "needs_fix", "findings": []})
        + "\nSuggested fix: `if not cfg: return {}`"
    )
    assert cr._declared_verdict(raw) == "needs_fix"


def test_verdict_in_a_fenced_block() -> None:
    raw = "Looks fine: `x = {}`.\n```json\n{\"status\": \"COMPLETED\", \"verdict\": \"escalate\"}\n```\n"
    assert cr._declared_verdict(raw) == "escalate"


@pytest.mark.parametrize("value,expected", [
    ("approved", ""), ("pass", ""), ("needs fix", "needs_fix"), ("Needs-Fix", "needs_fix"),
    ("CLEAN", "clean"), ("", ""),
])
def test_verdict_outside_the_allowed_set_is_no_verdict(value: str, expected: str) -> None:
    assert cr._declared_verdict(json.dumps({"verdict": value})) == expected


def test_failed_review_with_a_clean_verdict_is_review_failed(run: Any) -> None:
    raw = json.dumps({"status": "FAILED", "verdict": "clean", "findings": []})
    draft, missions = run(WorkerResult(task_id="t", status="FAILED", summary="timeout", raw=raw))
    assert draft.code_review and draft.code_review.status == "review_failed"
    assert missions == ["pr_review"]


def test_partial_review_with_a_clean_verdict_escalates(run: Any) -> None:
    raw = json.dumps({"status": "PARTIAL", "verdict": "clean", "findings": []})
    draft, _ = run(WorkerResult(task_id="t", status="PARTIAL", summary="", raw=raw))
    assert draft.code_review and draft.code_review.status == "escalate"
    assert draft.action != "merge_ready"


def test_needs_fix_without_findings_escalates(run: Any) -> None:
    raw = json.dumps({"status": "COMPLETED", "verdict": "needs_fix", "findings": []})
    draft, missions = run(WorkerResult(task_id="t", status="COMPLETED", summary="", raw=raw))
    assert draft.code_review and draft.code_review.status == "escalate"
    assert draft.action != "merge_ready"
    assert missions == ["pr_review"]


def test_completed_without_verdict_or_findings_stays_clean(run: Any) -> None:
    raw = json.dumps({"status": "COMPLETED", "findings": []})
    draft, _ = run(WorkerResult(task_id="t", status="COMPLETED", summary="", raw=raw))
    assert draft.code_review and draft.code_review.status == "clean"
