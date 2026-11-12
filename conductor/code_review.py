"""Optional draft-PR code-review phase (off/standard/high/max)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from conductor.draft_pr import annotate_pr_review
from conductor.missions import WorkerTask, parse_worker_json
from conductor.schemas import CodeReviewSummary, DraftPr
from conductor.workers import run_worker  # noqa: F401 — used via run_worker below

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReviewConfig:
    intensity_default: str = "high"
    intensity_by_mission: dict[str, str] | None = None
    max_fix_rounds: dict[str, int] | None = None


def load_review_config(path: str | Path = "config/code_review.yaml") -> ReviewConfig:
    p = Path(path)
    raw: dict[str, Any] = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return ReviewConfig(
        intensity_default=str(raw.get("intensity_default") or "high"),
        intensity_by_mission=dict(raw.get("intensity_by_mission") or {}),
        max_fix_rounds=dict(raw.get("max_fix_rounds") or {}),
    )


def intensity_for(mission: str, cfg: ReviewConfig | None = None) -> str:
    cfg = cfg or load_review_config()
    by = cfg.intensity_by_mission or {}
    return str(by.get(mission) or cfg.intensity_default or "high")


def max_rounds(intensity: str, cfg: ReviewConfig | None = None) -> int:
    cfg = cfg or load_review_config()
    rounds = cfg.max_fix_rounds or {}
    return int(rounds.get(intensity, 0 if intensity == "off" else 1))


def review_draft_pr(
    draft: DraftPr,
    *,
    mission: str,
    repo_path: str,
    dry_run: bool = False,
    skip: bool = False,
) -> DraftPr:
    """Run a fresh reviewer worker (not the author). Updates draft.action / code_review."""
    cfg = load_review_config()
    intensity = intensity_for(mission, cfg)
    if intensity == "off" or skip:
        return annotate_pr_review(
            draft,
            CodeReviewSummary(
                intensity=intensity if intensity != "off" else "off",
                status="review_skipped_time_budget" if skip else "skipped",
                findings_count=0,
                fix_rounds=0,
            ),
        )

    rounds_left = max_rounds(intensity, cfg)
    fix_rounds = 0
    status = "needs_fix"
    findings_count = 0

    while True:
        task = WorkerTask(
            id=f"review-{draft.repo}-{fix_rounds}",
            mission="pr_review",
            title=f"Review {draft.title}",
            repo=draft.repo,
            repo_path=repo_path,
            intensity=intensity,
            prompt=(
                f"Review draft PR {draft.url} for repo {draft.repo}.\n"
                f"Title: {draft.title}\nIntensity: {intensity}\n"
                f"Prior fix rounds: {fix_rounds}\n"
                "You are not the author. Be skeptical."
            ),
        )
        result = run_worker(task, dry_run=dry_run)
        if dry_run:
            status = "clean"
            findings_count = 0
            break

        parsed = result
        verdict = "needs_fix"
        # parse_worker_json already applied; look for verdict in raw
        from conductor.missions import parse_worker_json

        again = parse_worker_json(result.raw or result.summary, task.id)
        findings_count = len(again.findings or result.findings)
        raw_lower = (result.raw or "").lower()
        if "\"verdict\": \"clean\"" in raw_lower or '"verdict":"clean"' in raw_lower:
            verdict = "clean"
        elif result.status == "ESCALATE" or "escalate" in raw_lower:
            verdict = "escalate"
        elif findings_count == 0 and result.status == "COMPLETED":
            verdict = "clean"

        if verdict == "clean":
            status = "clean"
            break
        if verdict == "escalate" or rounds_left <= 0:
            status = "escalate"
            break

        # Fix round
        fix = WorkerTask(
            id=f"fix-{draft.repo}-{fix_rounds}",
            mission="pr_fix",
            title=f"Fix review findings for {draft.title}",
            repo=draft.repo,
            repo_path=repo_path,
            intensity=intensity,
            prompt=(
                f"Address reviewer findings on {draft.url}:\n"
                f"{json_dumps(again.findings or result.findings)}\n"
                "Smallest patch only."
            ),
        )
        run_worker(fix, dry_run=dry_run)
        fix_rounds += 1
        rounds_left -= 1

    return annotate_pr_review(
        draft,
        CodeReviewSummary(
            intensity=intensity,
            status=status,
            findings_count=findings_count,
            fix_rounds=fix_rounds,
        ),
    )


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=2)[:4000]
