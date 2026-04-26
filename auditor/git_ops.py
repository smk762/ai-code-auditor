"""Git automation for the repair pipeline.

Public API
----------
create_repair_branch(repo_path, slug, base_branch=None)   -> BranchResult
commit_repair(repo_path, message, patch_text="", ...)     -> CommitResult
push_repair_branch(repo_path, branch=None, remote=...)    -> PushResult
rollback_repair_branch(repo_path, original, repair, ...)  -> None (best-effort)
apply_to_tempdir(repo_path, patch_text)                   -> (Path, cleanup_fn)

Helpers used by the audit API
------------------------------
is_git_repo(path), get_current_branch(repo_path),
is_working_tree_clean(repo_path), is_in_conflict_state(repo_path),
get_remote_url(repo_path, remote), sanitize_branch_slug(text)

All public functions are synchronous and safe to call from asyncio
via loop.run_in_executor.  All raise GitError (or a subclass) on
unrecoverable failure; callers should decide whether to abort or continue.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_GIT_TIMEOUT = 30   # seconds — local operations
_PUSH_TIMEOUT = 90  # seconds — network push; leave headroom for slow links


# ── Exceptions ────────────────────────────────────────────────────────────────

class GitError(RuntimeError):
    """Base class for all git operation failures."""

class GitConflictError(GitError):
    """Raised when the repo is mid-merge, mid-rebase, or mid-cherry-pick."""

class GitNotCleanError(GitError):
    """Raised when an operation requires a clean working tree but finds one dirty."""

class GitNoRemoteError(GitError):
    """Raised when push is requested but no remote is configured."""


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class BranchResult:
    branch: str       # e.g. "repair/ext-retry-core-001"
    base_branch: str  # the branch (or short SHA) we forked from
    is_new: bool      # False when the branch already existed and we just switched to it

@dataclass(slots=True)
class CommitResult:
    sha: str            # full 40-char hash, or "" when nothing was staged
    branch: str         # branch the commit landed on
    files_staged: int   # number of files in the commit (0 → nothing to commit)
    short_message: str  # first line of commit message (≤72 chars)

@dataclass(slots=True)
class PushResult:
    pushed: bool
    remote: str
    branch: str
    remote_url: str
    skipped_reason: str = ""  # non-empty when pushed=False


# ── Low-level subprocess wrapper ──────────────────────────────────────────────

def _git(
    args: list[str],
    cwd: Path,
    *,
    timeout: int = _GIT_TIMEOUT,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run git with *args* in *cwd*.  Raises GitError when check=True and rc != 0."""
    run_env = {**os.environ, **(extra_env or {})}
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]!r} timed out after {timeout}s in {cwd}")
    except OSError as exc:
        raise GitError(f"git not found or not executable: {exc}") from exc
    if check and result.returncode != 0:
        stderr = result.stderr.strip()[:800]
        raise GitError(
            f"git {' '.join(args)!r} failed (rc={result.returncode}): {stderr}"
        )
    return result


# ── Public introspection helpers ──────────────────────────────────────────────

def is_git_repo(path: Path) -> bool:
    """True if *path* is the root of a git working tree or bare repo."""
    if (path / ".git").exists():
        return True
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-bare-repository"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def get_current_branch(repo_path: Path) -> str:
    """Return the checked-out branch name.

    Returns the short commit SHA when HEAD is detached, or "" on any error.
    """
    try:
        r = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path, check=False)
        name = r.stdout.strip()
        if name == "HEAD":
            sha_r = _git(["rev-parse", "HEAD"], repo_path, check=False)
            return sha_r.stdout.strip()[:12]
        return name
    except GitError:
        return ""


def is_working_tree_clean(repo_path: Path) -> bool:
    """True when there are no staged or unstaged modifications.

    Untracked files are ignored — we only care about what git tracks.
    """
    try:
        r = _git(["status", "--porcelain"], repo_path)
        tracked_changes = [l for l in r.stdout.splitlines() if not l.startswith("??")]
        return len(tracked_changes) == 0
    except GitError:
        return False


def is_in_conflict_state(repo_path: Path) -> bool:
    """True when the repo is mid-merge, mid-rebase, or mid-cherry-pick."""
    git_dir = repo_path / ".git"
    return any(
        (git_dir / marker).exists()
        for marker in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
        )
    )


def get_remote_url(repo_path: Path, remote: str = "origin") -> str | None:
    """Return the fetch URL for *remote*, or None if no remote is configured."""
    try:
        r = _git(["remote", "get-url", remote], repo_path, check=False)
        if r.returncode == 0:
            url = r.stdout.strip()
            return url or None
        return None
    except GitError:
        return None


def sanitize_branch_slug(text: str) -> str:
    """Convert arbitrary text to a safe git branch name component.

    Transform: lowercase, spaces/underscores → dashes, strip non-alnum/dash,
    collapse consecutive dashes, trim to 50 chars.
    """
    s = text.lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip("-")
    return s[:50]


# ── Branch management ─────────────────────────────────────────────────────────

def _branch_exists_local(repo_path: Path, branch: str) -> bool:
    try:
        r = _git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            repo_path, check=False,
        )
        return r.returncode == 0
    except GitError:
        return False


def _pick_free_branch_name(repo_path: Path, slug: str) -> str:
    """Return ``repair/<slug>`` if free; otherwise ``repair/<slug>-N`` (N=1..99)."""
    base = f"repair/{slug}"
    if not _branch_exists_local(repo_path, base):
        return base
    for n in range(1, 100):
        candidate = f"{base}-{n}"
        if not _branch_exists_local(repo_path, candidate):
            return candidate
    raise GitError(
        f"All branch names under repair/{slug}{{,-0..99}} are taken. "
        "Clean up old repair branches."
    )


def create_repair_branch(
    repo_path: Path,
    slug: str,
    base_branch: str | None = None,
) -> BranchResult:
    """Create and check out a ``repair/<slug>`` branch in *repo_path*.

    Args:
        repo_path:    Root of the git working tree.
        slug:         Arbitrary label sanitised into the branch name.
        base_branch:  Branch to fork from.  Defaults to current HEAD.

    Returns:
        BranchResult.

    Raises:
        GitError:         If branch creation fails.
        GitConflictError: If the repo is mid-merge/rebase/cherry-pick.
    """
    if not is_git_repo(repo_path):
        raise GitError(f"Not a git repository: {repo_path}")

    if is_in_conflict_state(repo_path):
        raise GitConflictError(
            f"{repo_path} is in a conflicted state "
            "(merge / rebase / cherry-pick in progress). "
            "Resolve conflicts before creating a repair branch."
        )

    original_branch = get_current_branch(repo_path)
    safe_slug = sanitize_branch_slug(slug) or "repair"

    # Optionally switch to a different base first
    if base_branch and base_branch != original_branch:
        try:
            _git(["checkout", base_branch], repo_path)
        except GitError as exc:
            raise GitError(
                f"Cannot check out base branch {base_branch!r}: {exc}"
            ) from exc
        effective_base = base_branch
    else:
        effective_base = original_branch or "HEAD"

    branch_name = _pick_free_branch_name(repo_path, safe_slug)

    try:
        _git(["checkout", "-b", branch_name], repo_path)
        log.info(
            "repair branch %r created from %r in %s",
            branch_name, effective_base, repo_path.name,
        )
        return BranchResult(branch=branch_name, base_branch=effective_base, is_new=True)
    except GitError:
        # Roll back: try to restore whatever branch we were on before
        _best_effort_checkout(repo_path, original_branch)
        raise


# ── Staging and committing ────────────────────────────────────────────────────

_SECRET_RE = re.compile(
    r"(\.env$|\.env\.|\.secret|credential|private_key|"
    r"id_rsa|id_ed25519|id_ecdsa|\.pem$|\.p12$|\.pfx$|"
    r"\.key$|auth\.json|token\.json)",
    re.IGNORECASE,
)


def _is_stageable(file_path: str) -> bool:
    """False for paths that look like secrets, keys, or credentials."""
    return not bool(_SECRET_RE.search(file_path))


def _extract_filenames_from_patch(patch_text: str) -> list[str]:
    """Parse the b-side filenames from a unified diff without a full parser.

    Handles both ``+++ b/path`` (git diff) and ``+++ path`` (plain diff).
    Skips ``/dev/null`` (deleted files — no file to stage).
    """
    files: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip()
        if path.startswith("b/"):
            path = path[2:]
        if path and path != "/dev/null":
            files.append(path)
    return files


def commit_repair(
    repo_path: Path,
    message: str,
    patch_text: str = "",
    author_name: str = "ai-code-auditor",
    author_email: str = "noreply@ai-audit.local",
) -> CommitResult:
    """Stage repair-touched files and commit them.

    Staging strategy:
    - If *patch_text* is provided: stage exactly the files named in the diff.
    - Otherwise: stage all modifications to already-tracked files (``git add -u``).
    Neither path stages .env / key / credential files.

    Returns a CommitResult with sha="" and files_staged=0 when nothing is staged
    (e.g. the patch was already committed, or the working tree is clean).

    Raises:
        GitError: If the commit subprocess fails for any reason other than an empty stage.
    """
    branch = get_current_branch(repo_path)

    if patch_text:
        patch_files = [f for f in _extract_filenames_from_patch(patch_text) if _is_stageable(f)]
        if not patch_files:
            log.info("commit_repair: patch produced no stageable files in %s", repo_path.name)
            return CommitResult(sha="", branch=branch, files_staged=0,
                                short_message=message.splitlines()[0][:72])
        # Stage only the files the patch touched — never git add -A
        for f in patch_files:
            # add --force in case the file is ignored (shouldn't happen for source files)
            _git(["add", "--", f], repo_path, check=False)
    else:
        # Fallback: stage modifications to tracked files + tracked deletions
        _git(["add", "-u"], repo_path)

    # Count what ended up staged
    staged_raw = _git(["diff", "--cached", "--name-only"], repo_path).stdout.strip()
    staged_files = [l for l in staged_raw.splitlines() if l.strip()]

    if not staged_files:
        log.info("commit_repair: nothing staged in %s", repo_path.name)
        return CommitResult(sha="", branch=branch, files_staged=0,
                            short_message=message.splitlines()[0][:72])

    author_env = {
        "GIT_AUTHOR_NAME":     author_name,
        "GIT_AUTHOR_EMAIL":    author_email,
        "GIT_COMMITTER_NAME":  author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    try:
        _git(["commit", "-m", message], repo_path, extra_env=author_env)
    except GitError as exc:
        # Unstage everything so the working tree is left in a known state
        _git(["reset", "HEAD"], repo_path, check=False)
        raise GitError(f"commit failed: {exc}") from exc

    sha = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()
    short_msg = message.splitlines()[0][:72]
    log.info(
        "committed repair %s on %r (%d files): %s",
        sha[:12], branch, len(staged_files), short_msg,
    )
    return CommitResult(sha=sha, branch=branch, files_staged=len(staged_files),
                        short_message=short_msg)


# ── Pushing ───────────────────────────────────────────────────────────────────

def _is_local_path_remote(url: str) -> bool:
    """True when the remote URL is a local filesystem path rather than a network remote.

    Network remote prefixes: ssh://, git://, https://, http://, git@host:
    Local examples: /path/to/bare.git, ../sibling-repo, file:///path
    """
    network_prefixes = ("ssh://", "git://", "https://", "http://", "git@")
    return not any(url.startswith(p) for p in network_prefixes)


def push_repair_branch(
    repo_path: Path,
    branch: str | None = None,
    remote: str = "origin",
) -> PushResult:
    """Push *branch* (default: current branch) to *remote*.

    Never force-pushes.  Returns PushResult(pushed=False) gracefully when:
    - No remote is configured.
    - The remote URL is a local filesystem path.
    - The push is rejected (auth failure, non-fast-forward, etc.).

    Raises GitError only for unexpected failures (git binary missing, etc.).
    """
    target_branch = branch or get_current_branch(repo_path)
    if not target_branch:
        return PushResult(
            pushed=False, remote=remote, branch="", remote_url="",
            skipped_reason="could not determine current branch",
        )

    remote_url = get_remote_url(repo_path, remote)
    if remote_url is None:
        log.info("push: no remote %r in %s — skipping", remote, repo_path.name)
        return PushResult(
            pushed=False, remote=remote, branch=target_branch, remote_url="",
            skipped_reason=f"no remote {remote!r} configured",
        )

    if _is_local_path_remote(remote_url):
        log.info("push: remote %r is a local path — skipping (%s)", remote, remote_url)
        return PushResult(
            pushed=False, remote=remote, branch=target_branch, remote_url=remote_url,
            skipped_reason="remote is a local filesystem path — push skipped",
        )

    try:
        _git(
            ["push", "--set-upstream", remote, target_branch],
            repo_path,
            timeout=_PUSH_TIMEOUT,
        )
        log.info("pushed %r → %r (%s)", target_branch, remote, remote_url)
        return PushResult(pushed=True, remote=remote, branch=target_branch, remote_url=remote_url)
    except GitError as exc:
        reason = str(exc)[:400]
        log.warning("push failed for %r: %s", target_branch, reason)
        return PushResult(
            pushed=False, remote=remote, branch=target_branch,
            remote_url=remote_url, skipped_reason=reason,
        )


# ── Rollback ──────────────────────────────────────────────────────────────────

def rollback_repair_branch(
    repo_path: Path,
    original_branch: str,
    repair_branch: str,
    *,
    delete_branch: bool = True,
) -> None:
    """Best-effort: restore *original_branch* and optionally delete *repair_branch*.

    Errors are logged but never raised — rollback is advisory.
    """
    try:
        _best_effort_checkout(repo_path, original_branch)
    except Exception as exc:
        log.warning("rollback: could not restore %r: %s", original_branch, exc)
        return

    if delete_branch and repair_branch:
        try:
            _git(["branch", "-D", repair_branch], repo_path, check=False)
            log.info("rollback: deleted repair branch %r", repair_branch)
        except Exception as exc:
            log.warning("rollback: could not delete %r: %s", repair_branch, exc)


# ── Temp-copy apply (used by /audit/validate_patch) ──────────────────────────

def apply_to_tempdir(
    repo_path: Path,
    patch_text: str,
) -> tuple[Path, Callable[[], None]]:
    """Copy the repo to a temp directory and apply *patch_text* via ``git apply``.

    Returns ``(patched_copy_path, cleanup_fn)``.  Always call cleanup_fn when done,
    even on exception in the caller — the temp dir is not self-cleaning.

    Raises:
        RuntimeError: if ``git apply`` fails (e.g. patch does not apply cleanly).
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="ai-validate-patch-"))

    def cleanup() -> None:
        shutil.rmtree(tmp_root, ignore_errors=True)

    try:
        repo_copy = tmp_root / "repo"
        shutil.copytree(str(repo_path), str(repo_copy), symlinks=True)

        patch_file = tmp_root / "changes.patch"
        patch_file.write_text(patch_text, encoding="utf-8")

        r = subprocess.run(
            ["git", "apply", "--whitespace=fix", str(patch_file)],
            cwd=repo_copy,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git apply failed: {r.stderr.strip()[:600]}")

        return repo_copy, cleanup
    except Exception:
        cleanup()
        raise


# ── Internal helpers ──────────────────────────────────────────────────────────

def _best_effort_checkout(repo_path: Path, branch: str) -> None:
    """Attempt to check out *branch*; silently ignore failures."""
    if not branch:
        return
    try:
        _git(["checkout", branch], repo_path, check=False)
    except Exception as exc:
        log.debug("_best_effort_checkout %r failed: %s", branch, exc)
