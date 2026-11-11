"""Draft PR remediation via git_ops + gh."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auditor.git_ops import (
    GitError,
    commit_repair,
    create_repair_branch,
    push_repair_branch,
    sanitize_branch_slug,
)
from conductor.schemas import CodeReviewSummary, DraftPr

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DraftPrResult:
    draft: DraftPr | None
    branch: str = ""
    error: str = ""


def _gh_available() -> bool:
    try:
        r = subprocess.run(["gh", "--version"], capture_output=True, text=True, check=False)
        return r.returncode == 0
    except OSError:
        return False


def create_draft_pr(
    repo_path: Path,
    *,
    repo_name: str,
    title: str,
    body: str,
    slug: str,
    severity: str = "MEDIUM",
    action: str = "review",
    base_branch: str | None = None,
    dry_run: bool = False,
) -> DraftPrResult:
    """Create repair branch, empty/notes commit if needed, push, open draft PR."""
    if dry_run:
        return DraftPrResult(
            draft=DraftPr(
                url=f"https://example.invalid/{repo_name}/pull/0",
                title=title,
                severity=severity,
                repo=repo_name,
                action=action,  # type: ignore[arg-type]
            ),
            branch=f"repair/{sanitize_branch_slug(slug)}",
        )

    try:
        br = create_repair_branch(repo_path, slug, base_branch=base_branch)
    except GitError as exc:
        return DraftPrResult(draft=None, error=str(exc))

    intent = repo_path / "intent.md"
    try:
        intent.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        commit_repair(repo_path, message=f"audit: {title[:60]}", paths=["intent.md"])
        push = push_repair_branch(repo_path, branch=br.branch)
    except (GitError, OSError, TypeError, ValueError) as exc:
        return DraftPrResult(draft=None, branch=br.branch, error=str(exc))

    if not push.pushed and push.skipped_reason:
        logger.warning("push skipped: %s", push.skipped_reason)

    url = ""
    if _gh_available():
        r = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--title",
                title[:70],
                "--body",
                body[:4000],
                "--head",
                br.branch,
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode == 0:
            url = (r.stdout or "").strip().splitlines()[-1].strip()
        else:
            logger.warning("gh pr create failed: %s", (r.stderr or "")[:500])
            return DraftPrResult(
                draft=None,
                branch=br.branch,
                error=(r.stderr or "gh pr create failed")[:500],
            )
    else:
        return DraftPrResult(draft=None, branch=br.branch, error="gh CLI not available")

    return DraftPrResult(
        draft=DraftPr(
            url=url,
            title=title,
            severity=severity,
            repo=repo_name,
            action=action,  # type: ignore[arg-type]
        ),
        branch=br.branch,
    )


def annotate_pr_review(draft: DraftPr, review: CodeReviewSummary) -> DraftPr:
    draft.code_review = review
    if review.status == "clean":
        draft.action = "merge_ready"
    elif review.status in ("escalate", "review_skipped_time_budget"):
        draft.action = "blocked"
    else:
        draft.action = "review"
    return draft


def finding_to_pr_body(finding: dict[str, Any], worker_summary: str) -> str:
    return (
        "## Night conductor remediation\n\n"
        f"{worker_summary}\n\n"
        "### Finding\n"
        f"```json\n{json.dumps(finding, indent=2)[:3000]}\n```\n\n"
        "Draft PR — do not auto-merge.\n"
    )


def pr_number_from_url(url: str) -> str | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return m.group(1) if m else None
