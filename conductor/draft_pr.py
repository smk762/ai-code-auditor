"""Draft PR remediation via git_ops + gh."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
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

# gh must never wait on a prompt or a stalled network: the conductor worker runs unattended.
GH_TIMEOUT_S = 120


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


def _git_out(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def scratch_clone(repo_path: Path, base_branch: str | None) -> Path:
    """A throwaway clone of *repo_path* checked out at the base, for repair work.

    *repo_path* is usually a mirror the scans read, and nothing may be written there (CLAUDE.md):
    a repair branch checked out in it was scanned instead of ``dev``, and its commits blocked the
    sync. The clone borrows objects (``--shared``), starts from the source's ``origin/<base>``
    (else its ``<base>``, else ``HEAD``), and takes over its ``origin`` URLs and committer, so the
    push and ``gh`` behave as in the source. The caller deletes ``result.parent``.
    """
    src = Path(repo_path)
    tmp = Path(tempfile.mkdtemp(prefix="draft-pr-"))
    clone = tmp / (src.name or "repo")
    try:
        r = subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(src), str(clone)],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise GitError(f"clone of {src} failed: {(r.stderr or '').strip()[:300]}")
        sha = ""
        for ref in ([f"refs/remotes/origin/{base_branch}", f"refs/heads/{base_branch}"] if base_branch else []) + ["HEAD"]:
            sha = _git_out(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], src)
            if sha:
                break
        if not sha:
            raise GitError(f"no base commit for {base_branch or 'HEAD'} in {src}")
        name = base_branch if base_branch and not base_branch.startswith("-") else "base"
        for args in (["checkout", "--quiet", "-B", name, sha], ["remote", "remove", "origin"]):
            r = subprocess.run(["git", *args], cwd=str(clone), capture_output=True, text=True, check=False)
            if r.returncode != 0:
                raise GitError(f"git {' '.join(args[:2])} in scratch clone failed: {(r.stderr or '').strip()[:300]}")
        url = _git_out(["config", "--get", "remote.origin.url"], src)
        if url:
            subprocess.run(["git", "remote", "add", "origin", url], cwd=str(clone), check=True, capture_output=True)
            push_url = _git_out(["config", "--get", "remote.origin.pushurl"], src)
            if push_url:
                subprocess.run(["git", "config", "remote.origin.pushurl", push_url], cwd=str(clone), check=True,
                               capture_output=True)
        for key in ("user.name", "user.email"):
            if val := _git_out(["config", "--local", "--get", key], src):
                subprocess.run(["git", "config", key, val], cwd=str(clone), check=True, capture_output=True)
        return clone
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


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
    draft: bool = True,
) -> DraftPrResult:
    """Create repair branch, notes commit, push, open draft PR: all in a throwaway clone of
    *repo_path* (``scratch_clone``), which is never itself branched, committed to or checked out.
    ``draft=False`` opens it ready for review: only where trust-gated autonomy allows (#61)."""
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
        work = scratch_clone(repo_path, base_branch)
    except (GitError, OSError, subprocess.SubprocessError) as exc:
        return DraftPrResult(draft=None, error=f"scratch clone failed: {exc}")
    try:
        return _draft_pr_in(work, repo_name=repo_name, title=title, body=body, slug=slug,
                            severity=severity, action=action, base_branch=base_branch, draft=draft)
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def _draft_pr_in(
    repo_path: Path,
    *,
    repo_name: str,
    title: str,
    body: str,
    slug: str,
    severity: str,
    action: str,
    base_branch: str | None,
    draft: bool = True,
) -> DraftPrResult:
    """The draft PR flow inside *repo_path*, a scratch clone already at the base.

    ``gh`` is checked before anything is pushed, and a pushed branch whose PR could not be
    opened is deleted again, so a failed run leaves no orphan branch on the remote."""
    if not _gh_available():
        return DraftPrResult(draft=None, error="gh CLI not available")
    try:
        br = create_repair_branch(repo_path, slug)
    except GitError as exc:
        return DraftPrResult(draft=None, error=str(exc))

    # Namespaced notes path, so a repo's own intent.md is never overwritten.
    intent_rel = f".audit/{sanitize_branch_slug(slug) or 'repair'}/intent.md"
    intent = repo_path / intent_rel
    try:
        intent.parent.mkdir(parents=True, exist_ok=True)
        intent.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        commit_repair(repo_path, message=f"audit: {title[:60]}", paths=[intent_rel])
        push = push_repair_branch(repo_path, branch=br.branch)
    except (GitError, OSError, TypeError, ValueError) as exc:
        return DraftPrResult(draft=None, branch=br.branch, error=str(exc))

    if not push.pushed:
        # gh pr create --head would fail on a branch the remote doesn't have; report why.
        reason = push.skipped_reason or "push did not happen"
        logger.warning("push skipped: %s", reason)
        return DraftPrResult(draft=None, branch=br.branch, error=f"push skipped: {reason}")

    url = ""
    try:
        r = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                *(["--draft"] if draft else []),
                "--title",
                title[:70],
                "--body",
                body[:4000],
                "--head",
                br.branch,
                *(["--base", base_branch] if base_branch else []),
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=GH_TIMEOUT_S,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
    except subprocess.TimeoutExpired:
        error = f"gh pr create timed out after {GH_TIMEOUT_S}s"
    else:
        lines = (r.stdout or "").strip().splitlines()
        url = lines[-1].strip() if lines else ""
        if r.returncode != 0:
            logger.warning("gh pr create failed: %s", (r.stderr or "")[:500])
            error = (r.stderr or "gh pr create failed")[:500]
        elif not url:
            error = "gh pr create printed no PR URL"
        else:
            error = ""
    if error:
        _delete_pushed_branch(repo_path, push.remote, br.branch)
        return DraftPrResult(draft=None, branch=br.branch, error=error)

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


def _delete_pushed_branch(repo_path: Path, remote: str, branch: str) -> None:
    """Best effort: delete *branch* on *remote* again, only while it still points at the commit
    this run pushed (``--force-with-lease``), so a branch someone else moved is left alone."""
    sha = _git_out(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo_path)
    if not sha:
        return
    try:
        r = subprocess.run(
            ["git", "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{sha}", "--", remote,
             f":refs/heads/{branch}"],
            cwd=str(repo_path), capture_output=True, text=True, check=False, timeout=GH_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        # It runs after a failed `gh pr create`: raising here would hide that error from the caller.
        logger.warning("could not delete orphan branch %s on %s: %s", branch, remote, exc)
        return
    if r.returncode != 0:
        logger.warning("could not delete orphan branch %s on %s: %s", branch, remote, (r.stderr or "")[:300])


def annotate_pr_review(draft: DraftPr, review: CodeReviewSummary) -> DraftPr:
    draft.code_review = review
    if review.status == "clean":
        draft.action = "merge_ready"
    elif review.status in ("escalate", "review_skipped_time_budget"):
        draft.action = "blocked"
    else:
        draft.action = "review"
    return draft


# A host path that survived the repo-root rewrite (another mirror, a home dir): only its file name is kept.
_HOST_PATH_RE = re.compile(r"(?<![\w/.~-])(?:/home/[^/\s]+|/Users/[^/\s]+|/root(?![\w.-]))(?:/[^\s\"'`<>()\[\]{},;]*)?")
# Field values are cut before serialising (never the JSON itself), so the block stays whole and the body
# under the 4000 characters `gh pr create` is given: each limit is tried until the JSON fits.
_FIELD_CHAR_LIMITS = (600, 200, 60)
_MAX_FINDING_JSON = 3000
_MAX_LIST_ITEMS = 20


def _redact(text: str, repo_path: str = "") -> str:
    """*text* with nothing about the machine that scanned it (#128): the repo root dropped wherever it
    appears, any other home-directory path cut to its file name, and the audit API's base URL removed."""
    root = (repo_path or "").rstrip("/")
    if root:
        text = text.replace(root + "/", "")
        text = re.sub(re.escape(root) + r"(?![\w.-])", ".", text)
    base = os.getenv("AI_AUDIT_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        text = text.replace(base, "<audit API>")
    return _HOST_PATH_RE.sub(lambda m: Path(m.group(0)).name or "<home>", text)


def _public_value(key: str, value: Any, repo_path: str, limit: int) -> Any:
    if isinstance(value, str):
        if value.startswith("/") and key in ("file", "file_path", "path") and not (
            repo_path and value.startswith(repo_path.rstrip("/") + "/")
        ):
            value = Path(value).name  # an absolute path outside the repo: keep only the file name
        value = _redact(value, repo_path)
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _public_value(str(k), v, repo_path, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_value(key, v, repo_path, limit) for v in list(value)[:_MAX_LIST_ITEMS]]
    return value


def _public_finding(
    finding: dict[str, Any], repo_path: str = "", limit: int = _FIELD_CHAR_LIMITS[0]
) -> dict[str, Any]:
    """*finding* fit for a public PR body: host paths made repo-relative wherever they appear (free text,
    nested lists and dicts too), so the body names files the way the repo does and says nothing about the
    machine that scanned it (#128). Long strings are cut here, before serialising."""
    return {k: _public_value(str(k), v, repo_path, limit) for k, v in finding.items()}


def _fence(text: str) -> str:
    """A backtick fence longer than any backtick run in *text*, so the text can't close it early."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def finding_to_pr_body(
    finding: dict[str, Any],
    worker_summary: str,
    *,
    pack_id: str = "",
    repo_path: str = "",
) -> str:
    """The draft PR body. It is public: packs are named by id and never linked, because the audit
    API's base URL is a private host (#128). The worker's summary is free text, so it gets the same
    redaction as the finding."""
    pack_lines = ""
    if pack_id:
        pack_lines = (
            "\n### Agent work pack\n"
            f"- id: `{pack_id}` (look it up in the audit API's pack list)\n"
        )
    for limit in _FIELD_CHAR_LIMITS:
        payload = json.dumps(_public_finding(finding, repo_path, limit), indent=2)
        if len(payload) <= _MAX_FINDING_JSON:
            break
    fence = _fence(payload)
    return (
        "## Night conductor remediation\n\n"
        f"{_redact(worker_summary or '', repo_path)}\n\n"
        "### Finding\n"
        f"{fence}json\n{payload}\n{fence}\n"
        f"{pack_lines}\n"
        "Draft PR — do not auto-merge.\n"
    )


def pr_number_from_url(url: str) -> str | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return m.group(1) if m else None
