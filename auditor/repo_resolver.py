from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from pathlib import Path

from auditor.config import RepoConfig, get_secret_from_env

logger = logging.getLogger(__name__)

# Tried in order when no branch is explicitly configured.
BRANCH_PREFERENCE: tuple[str, ...] = ("dev", "main", "master")

# Seconds before a local git query (rev-parse, diff) or a network one (clone, fetch) is abandoned.
GIT_LOCAL_TIMEOUT_S = 60
GIT_NETWORK_TIMEOUT_S = 600

# A cache directory name: one path segment, so ``cache_root / name`` stays inside the cache.
_SAFE_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# The userinfo of a URL (``https://user:token@host``), masked in error text.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")


def head_commit(repo_path: Path) -> dict[str, str]:
    """The checked-out commit a scan reads: ``{"sha", "committed_at"}``, or {} if not a git repo."""
    try:
        out = subprocess.run(["git", "-C", str(repo_path), "log", "-1", "--format=%H%x1f%cI"],
                             capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {}
    sha, _, committed_at = out.partition("\x1f")
    return {"sha": sha, "committed_at": committed_at} if sha and committed_at else {}


def branch_exists(repo_path: Path, branch: str) -> bool:
    """Return True if *branch* exists as a local branch in the git repo at *repo_path*."""
    # show-ref matches the literal ref name; rev-parse would also accept revision
    # expressions such as ``dev~1`` or ``dev^{tree}``.
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            timeout=GIT_LOCAL_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
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
    if base.startswith("-") or head.startswith("-"):
        # A leading dash would make git read the range as an option (e.g. --output=<file>).
        logger.warning("diff_files: refusing ref that looks like a git option: %r...%r", base, head)
        return frozenset()
    try:
        result = subprocess.run(
            # quotePath off and -z: paths come back raw, not C-quoted, and need no stripping.
            ["git", "-C", str(repo_path), "-c", "core.quotePath=false", "diff", "--name-only", "-z",
             f"{base}...{head}"],
            capture_output=True,
            timeout=GIT_LOCAL_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    out = result.stdout.decode("utf-8", errors="surrogateescape")
    return frozenset(p for p in out.split("\0") if p)


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


def _cache_dir(repo: RepoConfig) -> tuple[Path, Path]:
    """``(cache_root, repo_dir)`` for *repo*; refuses a name that would leave the cache."""
    name = repo.name or ""
    if not _SAFE_REPO_NAME.match(name) or name in (".", ".."):
        msg = f"repo name {name!r} is not a safe cache directory name"
        raise ValueError(msg)
    cache_root = Path(repo.local_cache_path or ".cache/repos")
    return cache_root, cache_root / name


def _run_git(args: list[str], git_env: dict[str, str], repo_name: str, token: str = "") -> None:
    """Run a network/cache git command, raising a sanitised RuntimeError on failure or timeout."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, env=git_env,
                                timeout=GIT_NETWORK_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        # "from None": the TimeoutExpired carries the full command and partial output, unredacted,
        # and a chained cause would print them wherever the traceback is logged.
        msg = f"git operation timed out for repo '{repo_name}' after {GIT_NETWORK_TIMEOUT_S}s"
        raise RuntimeError(msg) from None
    _ensure_git_ok(result, repo_name, token)


def _materialize_remote_repo(repo: RepoConfig) -> Path:
    cache_root, repo_dir = _cache_dir(repo)
    cache_root.mkdir(parents=True, exist_ok=True)
    token = get_secret_from_env(repo.access_token_env)
    git_env = _git_env(token, cache_root)
    if repo_dir.exists() and not (repo_dir / ".git").exists():
        # Not a clone (a failed clone, or made by hand): git -C would search upward and act on an
        # enclosing repository, so replace it. repo_dir is a direct child of the cache.
        logger.warning("repo '%s': cache dir %s is not a git clone; re-cloning", repo.name, repo_dir)
        shutil.rmtree(repo_dir, ignore_errors=True)
    if not repo_dir.exists():
        _clone_with_branch_fallback(repo, git_env)
        return repo_dir
    # The cache is a disposable copy: move it to the remote tip even after a force-push, and
    # discard any local change, rather than a pull --ff-only that would then fail on every run.
    branch = select_branch(repo, repo_dir)
    remote_ref = f"refs/remotes/origin/{branch}"
    _run_git(["git", "-C", str(repo_dir), "fetch", "--depth", "1", "origin",
              f"+refs/heads/{branch}:{remote_ref}"], git_env, repo.name, token)
    _run_git(["git", "-C", str(repo_dir), "checkout", "-f", "-B", branch, remote_ref],
             git_env, repo.name, token)
    _run_git(["git", "-C", str(repo_dir), "clean", "-fdx"], git_env, repo.name, token)
    return repo_dir


def _clone_with_branch_fallback(repo: RepoConfig, git_env: dict[str, str] | None) -> str:
    """Clone *repo* at ``repo.branch`` when one is configured, else the first of
    ``BRANCH_PREFERENCE`` that exists. A configured branch is never swapped for another.

    Returns the branch that succeeded.
    """
    cache_root, repo_dir = _cache_dir(repo)
    if repo.path.startswith("-"):
        msg = f"repo '{repo.name}' path must not start with '-'"
        raise ValueError(msg)
    if git_env is None:
        git_env = _git_env("", cache_root)
    token = git_env.get("GIT_TOKEN", "")
    candidates = [repo.branch] if repo.branch else list(BRANCH_PREFERENCE)
    existed = repo_dir.exists()
    last_error = ""
    for branch in candidates:
        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", branch, "--", repo.path, str(repo_dir)],
                capture_output=True,
                text=True,
                env=git_env,
                timeout=GIT_NETWORK_TIMEOUT_S, check=False,
            )
        except subprocess.TimeoutExpired:
            result = None
            last_error = f"timed out after {GIT_NETWORK_TIMEOUT_S}s"
        if result is not None and result.returncode == 0:
            logger.debug("repo '%s': cloned branch '%s'", repo.name, branch)
            return branch
        if result is not None:
            last_error = (result.stderr or "").strip()
        if repo_dir.exists() and not existed:
            # Clean up the partial clone before retrying; never a directory that was already there.
            shutil.rmtree(repo_dir, ignore_errors=True)
    raise RuntimeError(
        f"git clone failed for repo '{repo.name}' (tried {candidates}): "
        f"{_sanitize_git_output(last_error, token)[:400]}"
    )


_ASKPASS_LOCK = threading.Lock()
_ASKPASS_PATH: str | None = None
_ASKPASS_BODY = (
    "#!/usr/bin/env sh\n"
    "case \"$1\" in\n"
    "*Username*) echo \"oauth2\" ;;\n"
    "*Password*) echo \"$GIT_TOKEN\" ;;\n"
    "*) echo \"\" ;;\n"
    "esac\n"
)


def _owned_private(path: Path, *, is_dir: bool) -> bool:
    """*path* is ours alone: not a link, the expected type, owned by this user, and writable by
    no one else (a private dir is also unreadable to others)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    kind_ok = stat.S_ISDIR(st.st_mode) if is_dir else stat.S_ISREG(st.st_mode)
    others = 0o077 if is_dir else 0o022
    return kind_ok and st.st_uid == os.geteuid() and not (st.st_mode & others)


def _askpass_is_ours(path: str) -> bool:
    script = Path(path)
    if not (_owned_private(script.parent, is_dir=True) and _owned_private(script, is_dir=False)):
        return False
    try:
        return script.read_text(encoding="utf-8") == _ASKPASS_BODY
    except OSError:
        return False


def _askpass_script() -> str:
    """One askpass script per process, reused. It holds no secret: it echoes ``$GIT_TOKEN``.

    git runs it with the token in its environment, so it lives in a private ``mkdtemp`` dir, and
    the cached path is reused only while it is still the file we wrote: a temp cleaner may delete
    it, and another local user could then put their own file at that known path.
    """
    global _ASKPASS_PATH
    with _ASKPASS_LOCK:
        if _ASKPASS_PATH and _askpass_is_ours(_ASKPASS_PATH):
            return _ASKPASS_PATH
        if _ASKPASS_PATH:
            logger.warning("askpass script %s is gone or not ours; writing a new one", _ASKPASS_PATH)
        private_dir = Path(tempfile.mkdtemp(prefix="ai-audit-askpass-"))
        script = private_dir / "askpass.sh"
        fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_ASKPASS_BODY)
        _ASKPASS_PATH = str(script)
        return _ASKPASS_PATH


def _git_env(token: str, cache_root: Path) -> dict[str, str]:
    """Environment for cache git commands: never prompt, never search above the cache."""
    env = _git_env_with_token(token) or dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CEILING_DIRECTORIES"] = str(cache_root.resolve())
    return env


def _git_env_with_token(token: str) -> dict[str, str] | None:
    if not token:
        return None
    # Avoid placing token in command args or URL.
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = _askpass_script()
    env["GIT_TOKEN"] = token
    return env


def _sanitize_git_output(text: str, token: str = "") -> str:
    """Mask URL credentials and any known token value in git's output."""
    if token:
        text = text.replace(token, "***")
    return _URL_USERINFO.sub(r"\1***@", text)


def _ensure_git_ok(result: subprocess.CompletedProcess[str], repo_name: str, token: str = "") -> None:
    if result.returncode != 0:
        stderr = _sanitize_git_output((result.stderr or "").strip(), token)
        # Never include token values in error output.
        raise RuntimeError(f"git operation failed for repo '{repo_name}': {stderr[:400]}")
