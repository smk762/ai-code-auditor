from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from auditor.config import RepoConfig, get_secret_from_env


def resolve_repo_path(repo: RepoConfig) -> Path:
    if _is_remote(repo.path):
        return _materialize_remote_repo(repo)
    return Path(repo.path)


def _is_remote(path: str) -> bool:
    return path.startswith("http://") or path.startswith("https://")


def _materialize_remote_repo(repo: RepoConfig) -> Path:
    cache_root = Path(repo.local_cache_path or ".cache/repos")
    cache_root.mkdir(parents=True, exist_ok=True)
    repo_dir = cache_root / repo.name
    token = get_secret_from_env(repo.access_token_env)
    git_env = _git_env_with_token(token)
    if not repo_dir.exists():
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", repo.branch, repo.path, str(repo_dir)],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        _ensure_git_ok(result, repo.name)
    else:
        fetch = subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "origin"],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        _ensure_git_ok(fetch, repo.name)
        checkout = subprocess.run(
            ["git", "-C", str(repo_dir), "checkout", repo.branch],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        _ensure_git_ok(checkout, repo.name)
        pull = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only", "origin", repo.branch],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        _ensure_git_ok(pull, repo.name)
    return repo_dir


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
