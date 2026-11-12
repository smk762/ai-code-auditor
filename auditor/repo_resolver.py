from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import tempfile

from auditor.config import RepoConfig, get_secret_from_env

logger = logging.getLogger(__name__)

# Tried in order when no branch is explicitly configured.
BRANCH_PREFERENCE: tuple[str, ...] = ("dev", "main", "master")


def branch_exists(repo_path: Path, branch: str) -> bool:
    """Return True if *branch* exists as a local branch in the git repo at *repo_path*."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
    )
    return result.returncode == 0


def select_branch(repo: RepoConfig, repo_path: Path | None = None) -> str:
    """Return the branch to use for *repo*.

    If ``repo.branch`` is set, return it unchanged.  Otherwise probe the local
    git repo at *repo_path* (if provided and it is a git repo) for the first
    branch in ``BRANCH_PREFERENCE`` that exists, then fall back to the first
    preference unchanged.
    """
    if repo.branch:
        return repo.branch
    if repo_path is not None and (repo_path / ".git").exists():
        for candidate in BRANCH_PREFERENCE:
            if branch_exists(repo_path, candidate):
                logger.debug("repo '%s': auto-selected branch '%s'", repo.name, candidate)
                return candidate
    return BRANCH_PREFERENCE[0]


def detect_checked_out_branch(repo_path: Path) -> str:
    """Return the name of the currently checked-out branch, or empty string if not a git repo / detached HEAD."""
    if not (repo_path / ".git").exists():
        return ""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    name = result.stdout.strip()
    return "" if name == "HEAD" else name  # "HEAD" means detached


def diff_files(repo_path: Path, base: str, head: str = "HEAD") -> frozenset[str]:
    """Return repo-relative paths of files that differ between *base* and *head*.

    Returns an empty set if either ref is missing or the repo is not a git repo.
    """
    if not (repo_path / ".git").exists():
        return frozenset()
    result = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--name-only", f"{base}...{head}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return frozenset()
    return frozenset(p.strip() for p in result.stdout.splitlines() if p.strip())


def resolve_repo_path(repo: RepoConfig) -> Path:
    if _is_remote(repo.path):
        return _materialize_remote_repo(repo)
    return Path(repo.path)


def resolve_scan_root(repo: RepoConfig) -> Path:
    """Repository root, or ``subpath`` inside it when set (must stay under the repo root)."""
    base = resolve_repo_path(repo)
    raw = (repo.subpath or "").strip().replace("\\", "/")
    if not raw or raw == ".":
        return base
    candidate = Path(raw)
    if candidate.is_absolute():
        msg = f"repo '{repo.name}' subpath must be a relative path"
        raise ValueError(msg)
    resolved = (base / candidate).resolve()
    base_resolved = base.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        msg = f"repo '{repo.name}' subpath escapes repository root"
        raise ValueError(msg) from exc
    return resolved


def _is_remote(path: str) -> bool:
    return path.startswith("http://") or path.startswith("https://")


def _materialize_remote_repo(repo: RepoConfig) -> Path:
    cache_root = Path(repo.local_cache_path or ".cache/repos")
    cache_root.mkdir(parents=True, exist_ok=True)
    repo_dir = cache_root / repo.name
    token = get_secret_from_env(repo.access_token_env)
    git_env = _git_env_with_token(token)
    if not repo_dir.exists():
        branch = _clone_with_branch_fallback(repo, git_env)
    else:
        fetch = subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "origin"],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        _ensure_git_ok(fetch, repo.name)
        branch = select_branch(repo, repo_dir)
        checkout = subprocess.run(
            ["git", "-C", str(repo_dir), "checkout", branch],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        _ensure_git_ok(checkout, repo.name)
        pull = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only", "origin", branch],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        _ensure_git_ok(pull, repo.name)
    return repo_dir


def _clone_with_branch_fallback(repo: RepoConfig, git_env: dict[str, str] | None) -> str:
    """Clone *repo* trying ``repo.branch`` first, then ``BRANCH_PREFERENCE`` in order.

    Returns the branch that succeeded.
    """
    cache_root = Path(repo.local_cache_path or ".cache/repos")
    repo_dir = cache_root / repo.name
    candidates = [repo.branch] if repo.branch else list(BRANCH_PREFERENCE)
    last_error = ""
    for branch in candidates:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, repo.path, str(repo_dir)],
            capture_output=True,
            text=True,
            env=git_env,
        )
        if result.returncode == 0:
            logger.debug("repo '%s': cloned branch '%s'", repo.name, branch)
            return branch
        last_error = (result.stderr or "").strip()
        if repo_dir.exists():
            # Clean up partial clone before retrying.
            import shutil
            shutil.rmtree(repo_dir, ignore_errors=True)
    raise RuntimeError(
        f"git clone failed for repo '{repo.name}' (tried {candidates}): {last_error[:400]}"
    )


def _git_env_with_token(token: str) -> dict[str, str] | None:
    if not token:
        return None
    # Avoid placing token in command args or URL.
    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8")
    tmp.write("#!/usr/bin/env sh\n")
    tmp.write("case \"$1\" in\n")
    tmp.write("*Username*) echo \"oauth2\" ;;\n")
    tmp.write("*Password*) echo \"$GIT_TOKEN\" ;;\n")
    tmp.write("*) echo \"\" ;;\n")
    tmp.write("esac\n")
    tmp.flush()
    Path(tmp.name).chmod(0o700)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = tmp.name
    env["GIT_TOKEN"] = token
    return env


def _ensure_git_ok(result: subprocess.CompletedProcess[str], repo_name: str) -> None:
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # Never include token values in error output.
        raise RuntimeError(f"git operation failed for repo '{repo_name}': {stderr[:400]}")
