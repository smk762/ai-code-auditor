"""Git automation for the repair pipeline.

Public API
----------
create_repair_branch(repo_path, slug, base_branch=None)   -> BranchResult
commit_repair(repo_path, message, patch_text="", paths=None, ...) -> CommitResult
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

class ScanSourceError(GitError):
    """A write into a checkout the scans read (a mirror, or a repo's configured scan path)."""


_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def scan_source_paths() -> set[Path]:
    """Every checkout the scans read: ``config/repo_mirrors.yaml`` and ``config/ecosystem.yaml``,
    resolved, so an alias symlink names the same mirror. Scans, validation and the API only read
    these (CLAUDE.md); repair work happens in a throwaway clone (``conductor.draft_pr``)."""
    import yaml

    out: set[Path] = set()
    for name, key in (("repo_mirrors.yaml", "mirrors"), ("ecosystem.yaml", "repos")):
        try:
            data = yaml.safe_load((_CONFIG_DIR / name).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        for item in data.get(key) or []:
            if isinstance(item, dict) and item.get("path"):
                out.add(Path(str(item["path"])).expanduser().resolve())
    return out


def refuse_scan_source(repo_path: Path) -> None:
    """Raise ScanSourceError when *repo_path* is a checkout the scans read, or a path inside one
    (git acts on the enclosing work tree from any subdirectory)."""
    resolved = Path(repo_path).expanduser().resolve()
    if any(resolved == src or resolved.is_relative_to(src) for src in scan_source_paths()):
        raise ScanSourceError(
            f"{repo_path} is a scan source (mirror or configured repo path); never branch, commit or "
            "push there. Work in a throwaway clone (conductor.draft_pr.scratch_clone).")


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

_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization:\s*)(?:\S+\s+)?\S+")
_SECRET_CONFIG_RE = re.compile(r"(?i)^([^=]*(?:extraheader|password|token|secret)[^=]*=).*$")


def redact_secrets(text: str) -> str:
    """Drop URL userinfo (``https://user:token@host`` → ``https://***@host``) and auth header values."""
    text = _URL_USERINFO_RE.sub(r"\1***@", text or "")
    return _AUTH_HEADER_RE.sub(r"\1***", text)


def _redact_args(args: list[str]) -> str:
    """*args* as one string with credentials removed, including ``-c http.extraheader=…`` values."""
    out = []
    for a in args:
        m = _SECRET_CONFIG_RE.match(a)
        out.append(m.group(1) + "***" if m else redact_secrets(a))
    return " ".join(out)


def _git(
    args: list[str],
    cwd: Path,
    *,
    timeout: int = _GIT_TIMEOUT,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
    errors: str = "replace",
) -> subprocess.CompletedProcess:
    """Run git with *args* in *cwd*.  Raises GitError when check=True and rc != 0.

    ``errors="surrogateescape"`` keeps non-UTF-8 path bytes round-trippable, for output that is
    passed back to git as paths rather than shown to a person.
    """
    run_env = {**os.environ, **(extra_env or {})}
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors=errors,  # non-UTF-8 paths/messages must not escape as UnicodeDecodeError
            timeout=timeout,
            env=run_env,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]!r} timed out after {timeout}s in {cwd}")
    except OSError as exc:
        raise GitError(f"git not found or not executable: {exc}") from exc
    if check and result.returncode != 0:
        stderr = redact_secrets(result.stderr.strip())[:800]
        raise GitError(
            f"git {_redact_args(args)!r} failed (rc={result.returncode}): {stderr}"
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
    except (OSError, subprocess.SubprocessError):  # no git, or it hung: not a repo we can use
        return False


def get_current_branch(repo_path: Path) -> str:
    """Return the checked-out branch name.

    Returns the short commit SHA when HEAD is detached, or "" on any error.
    An unborn branch (no commits yet) returns its name from ``symbolic-ref``.
    """
    try:
        r = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path, check=False)
        if r.returncode != 0:
            # rev-parse echoes "HEAD" even when it fails, so its stdout is no name here.
            sym = _git(["symbolic-ref", "--short", "-q", "HEAD"], repo_path, check=False)
            return sym.stdout.strip() if sym.returncode == 0 else ""
        name = r.stdout.strip()
        if name == "HEAD":
            sha_r = _git(["rev-parse", "HEAD"], repo_path, check=False)
            return sha_r.stdout.strip()[:12] if sha_r.returncode == 0 else ""
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
    git_dir = _resolve_git_dir(repo_path)
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


def _resolve_git_dir(repo_path: Path) -> Path:
    """The real git dir: in a linked worktree or a submodule ``.git`` is a ``gitdir:`` file."""
    try:
        r = _git(["rev-parse", "--git-dir"], repo_path, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return (repo_path / r.stdout.strip()).resolve()
    except GitError:
        pass
    dot_git = repo_path / ".git"
    if dot_git.is_file():
        try:
            lines = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        if lines and lines[0].startswith("gitdir:"):
            return (repo_path / lines[0][len("gitdir:"):].strip()).resolve()
    return dot_git


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
    refuse_scan_source(repo_path)

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
        # A value starting with "-" would be read as a checkout option (e.g. --force).
        if base_branch.startswith("-") or any(c.isspace() for c in base_branch):
            raise GitError(f"Invalid base branch {base_branch!r}")
        if _git(["rev-parse", "--verify", "--quiet", f"{base_branch}^{{commit}}"],
                repo_path, check=False).returncode != 0:
            raise GitError(f"Cannot check out base branch {base_branch!r}: no such commit")
        try:
            _git(["checkout", base_branch, "--"], repo_path)
        except GitError as exc:
            raise GitError(
                f"Cannot check out base branch {base_branch!r}: {exc}"
            ) from exc
        effective_base = base_branch
    else:
        effective_base = original_branch or "HEAD"

    try:
        branch_name = _pick_free_branch_name(repo_path, safe_slug)
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
    """Parse the touched filenames from a unified diff without a full parser.

    Handles both ``+++ b/path`` (git diff) and ``+++ path`` (plain diff).
    For deletions (``+++ /dev/null``) falls back to the preceding
    ``--- a/<path>`` so the deleted file still gets staged.

    Hunk bodies are skipped using the ``@@`` line counts, so an added line whose
    content starts with ``++ `` is not taken for a header. Plain-diff timestamps
    (``+++ path<TAB>2024-01-01 …``) are dropped and git's C-quoted paths decoded.

    In a git diff a rename or copy names both sides, so the old path of a rename is staged too
    (its deletion belongs in the commit). Blocks with no ``---``/``+++`` pair (a pure rename or
    copy, a binary patch, a mode change) take their paths from ``rename from``/``rename to``/
    ``copy to`` lines or, failing those, from the ``diff --git`` header.
    """
    files: list[str] = []
    lines = patch_text.splitlines()
    in_git_block = block_is_copy = False
    block_header = ""      # the current "diff --git" line, until the block names a path
    i = 0

    def flush_header() -> None:
        nonlocal block_header
        if block_header:
            files.extend(p for p in _git_header_paths(block_header) if p != "/dev/null")
            block_header = ""

    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            flush_header()
            in_git_block, block_is_copy, block_header = True, False, line
            i += 1
            continue
        if in_git_block and line.startswith(("rename from ", "rename to ", "copy to ")):
            raw = line.split(" ", 2)[2]
            files.append(_header_path(raw, ""))
            block_header = ""
            i += 1
            continue
        if in_git_block and line.startswith("copy from "):
            block_header, block_is_copy = "", True  # the source is unchanged; only "copy to" is staged
            i += 1
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_left = int(m.group(2)) if m.group(2) is not None else 1
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0):
                body = lines[i]
                tag = body[:1]
                if tag == "-":
                    old_left -= 1
                elif tag == "+":
                    new_left -= 1
                elif tag in (" ", ""):
                    old_left -= 1
                    new_left -= 1
                elif tag != "\\":  # "\ No newline at end of file" counts for neither side
                    break
                i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            a_path = _header_path(line[4:], "a/")
            b_path = _header_path(lines[i + 1][4:], "b/")
            has_new = bool(b_path) and b_path != "/dev/null"
            if has_new:
                files.append(b_path)
            # A git rename with edits ("--- a/old" / "+++ b/new") stages the old path's deletion
            # too. In a plain diff "--- " may name a backup copy, so only a deletion
            # ("+++ /dev/null") takes the old side there.
            if a_path and a_path != "/dev/null" and not block_is_copy and (in_git_block or not has_new):
                files.append(a_path)
            block_header = ""
            i += 2
            continue
        i += 1
    flush_header()
    return list(dict.fromkeys(files))


def _git_header_paths(line: str) -> list[str]:
    """Both paths of a ``diff --git a/<old> b/<new>`` line.

    Quoted paths are unambiguous. Unquoted ones may hold spaces, so the line is split where the
    two halves agree (a non-rename) or at the only `` b/`` (a rename, whose names git then also
    gives in ``rename from``/``rename to`` lines handled by the caller).
    """
    rest = line[len("diff --git "):]
    if rest.startswith('"'):
        end = 1
        while end < len(rest) and rest[end] != '"':
            end += 2 if rest[end] == "\\" else 1
        old, new = rest[:end + 1], rest[end + 1:].strip()
        return [_header_path(old, "a/"), _header_path(new, "b/")]
    if rest.endswith('"') and ' "b/' in rest:
        old, new = rest.rsplit(' "b/', 1)
        return [_header_path(old, "a/"), _header_path('"b/' + new, "b/")]
    if rest.startswith("a/"):
        n = len(rest)
        # Same path both sides: "a/<p> b/<p>" has odd length 2*len(p) + 5.
        if n % 2 == 1:
            half = (n - 1) // 2
            old, new = rest[:half], rest[half + 1:]
            if old[2:] == new[2:] and new.startswith("b/"):
                return [old[2:]]
        if rest.count(" b/") == 1:
            old, new = rest.split(" b/")
            return [old[2:], new]
    return []


_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


def _c_unquote(s: str) -> str:
    """Decode git's C-quoted path (``"dir/na\\303\\257ve file.py"``) to the real name.

    Bytes that are not UTF-8 (a Latin-1 name from an old repo) decode as surrogate escapes, the
    way ``os.fsdecode`` does, so the path still names the file on disk and in the index.
    """
    body, out, i = s[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567" and re.match(r"[0-7]{3}", body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
        out += ch.encode("utf-8", errors="surrogateescape")
        i += 1
    return out.decode("utf-8", errors="surrogateescape")


def _header_path(raw: str, prefix: str) -> str:
    """The path in a ``---``/``+++`` header: timestamp cut, C-quoting undone, ``a/``/``b/`` dropped."""
    raw = raw.strip()
    if raw.startswith('"'):
        end = 1
        while end < len(raw):
            if raw[end] == "\\":
                end += 2
                continue
            if raw[end] == '"':
                break
            end += 1
        path = _c_unquote(raw[:end + 1])
    else:
        path = raw.split("\t", 1)[0].strip()
    return path.removeprefix(prefix)


def commit_repair(
    repo_path: Path,
    message: str,
    patch_text: str = "",
    author_name: str = "ai-code-auditor",
    author_email: str = "noreply@ai-audit.local",
    paths: list[str] | None = None,
) -> CommitResult:
    """Stage repair-touched files and commit them.

    Staging strategy:
    - If *paths* is provided: stage exactly those paths (after secret-path filter).
    - Else if *patch_text* is provided: stage exactly the files named in the diff.
    - Otherwise: stage modifications/deletions of already-tracked files.
    No branch stages .env / key / credential files or a path outside the repo,
    and only the selected files are committed: changes the index already held
    stay staged and out of the commit.

    Returns a CommitResult with sha="" and files_staged=0 when nothing is staged
    (e.g. the patch was already committed, or the working tree is clean).

    Raises:
        GitError: If the commit subprocess fails for any reason other than an empty stage.
        ScanSourceError: If *repo_path* is a checkout the scans read.
    """
    refuse_scan_source(repo_path)
    branch = get_current_branch(repo_path)
    short_msg = (message.splitlines() or [""])[0][:72]
    nothing = CommitResult(sha="", branch=branch, files_staged=0, short_message=short_msg)
    # Literal pathspecs: a file named "*.py" must not stage (or commit) every .py file.
    lit = ["--literal-pathspecs"]

    if paths:
        candidates = [f for f in paths if f]
        source = "paths"
    elif patch_text:
        candidates = _extract_filenames_from_patch(patch_text)
        source = "patch"
    else:
        # Fallback: modified + deleted tracked files, through the same secret filter.
        listed = _git(lit + ["diff", "--name-only", "-z"], repo_path, errors="surrogateescape").stdout
        candidates = [f for f in listed.split("\0") if f]
        source = "tracked changes"

    stageable = [f for f in dict.fromkeys(candidates) if _is_stageable(f)]
    if not stageable:
        log.info("commit_repair: %s produced no stageable files in %s", source, repo_path.name)
        return nothing
    root = repo_path.resolve()
    for f in stageable:
        # Never allow path escape out of the repo root, whichever branch named the file.
        try:
            (repo_path / f).resolve().relative_to(root)
        except ValueError as exc:
            raise GitError(f"refusing to stage path outside repo: {f!r}") from exc
    # The index as the user left it, restored whole if the commit fails: resetting our paths to
    # HEAD would also drop staging the user already had on them (e.g. partial hunks).
    index_backup = _backup_index(repo_path)
    try:
        return _stage_and_commit(repo_path, message, stageable, lit, nothing, branch, short_msg,
                                 author_name, author_email, index_backup)
    finally:
        if index_backup is not None:
            index_backup[1].unlink(missing_ok=True)


def _backup_index(repo_path: Path) -> tuple[Path, Path] | None:
    """(index path, copy of it), or None when there is no index file to copy."""
    r = _git(["rev-parse", "--git-path", "index"], repo_path, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    index = Path(r.stdout.strip())
    if not index.is_absolute():
        index = repo_path / index
    if not index.is_file():
        return None
    fd, tmp = tempfile.mkstemp(prefix="index-backup-", dir=index.parent)
    os.close(fd)
    shutil.copy2(index, tmp)
    return index, Path(tmp)


def _stage_and_commit(
    repo_path: Path,
    message: str,
    stageable: list[str],
    lit: list[str],
    nothing: CommitResult,
    branch: str,
    short_msg: str,
    author_name: str,
    author_email: str,
    index_backup: tuple[Path, Path] | None,
) -> CommitResult:
    for f in stageable:
        # "add -A -- <path>" stages a modification, an addition or a deletion of exactly that path.
        _git(lit + ["add", "-A", "--", f], repo_path, check=False)

    # Count only our files: anything else already in the index stays staged and is not committed.
    # --no-renames: with rename detection a move lists only its new path, and the old path's
    # deletion would stay staged and out of the commit.
    staged_raw = _git(lit + ["diff", "--cached", "--no-renames", "--name-only", "-z", "--", *stageable],
                      repo_path, errors="surrogateescape").stdout
    staged_files = [f for f in staged_raw.split("\0") if f.strip()]

    if not staged_files:
        log.info("commit_repair: nothing staged in %s", repo_path.name)
        return nothing

    author_env = {
        "GIT_AUTHOR_NAME":     author_name,
        "GIT_AUTHOR_EMAIL":    author_email,
        "GIT_COMMITTER_NAME":  author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    try:
        # "commit -- <paths>" commits only these paths, never a pre-existing staged change.
        _git(lit + ["commit", "-m", message, "--", *staged_files], repo_path, extra_env=author_env)
    except GitError as exc:
        # Put the index back as it was, so the user's own staging survives, on these paths too.
        if index_backup is not None:
            os.replace(index_backup[1], index_backup[0])
        else:
            _git(lit + ["reset", "-q", "--", *staged_files], repo_path, check=False)
        raise GitError(f"commit failed: {exc}") from exc

    sha = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()
    log.info(
        "committed repair %s on %r (%d files): %s",
        sha[:12], branch, len(staged_files), short_msg,
    )
    return CommitResult(sha=sha, branch=branch, files_staged=len(staged_files),
                        short_message=short_msg)


# ── Pushing ───────────────────────────────────────────────────────────────────

def _is_local_path_remote(url: str) -> bool:
    """True when the remote URL is a local filesystem path rather than a network remote.

    Follows git's own rules: ``file://`` is local; any other ``scheme://`` URL and
    any ``<transport>::<address>`` are not; scp-like ``[user@]host:path`` (no slash
    before the first colon) is network. Everything else is a filesystem path.
    Local examples: /path/to/bare.git, ../sibling-repo, file:///path, git@backup/repo.git
    """
    url = url.strip()
    if re.match(r"(?i)^file://", url):
        return True
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url) or re.match(r"^[A-Za-z0-9][A-Za-z0-9+.-]*::", url):
        return False
    colon = url.find(":")
    if colon > 0 and "/" not in url[:colon]:
        # A Windows drive ("C:\\repo", "C:/repo") is a path, not host "C".
        return colon == 1 and url[0].isalpha()
    return True


_PUSH_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_PUSH_REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _valid_push_name(name: str, *, remote: bool = False) -> bool:
    """A remote or branch name git can read only as that name.

    Beyond option injection (a leading ``-``) and argument splitting (whitespace), a branch is a
    refspec to ``git push`` (``+main`` forces, ``:main`` deletes, ``HEAD:refs/heads/x`` retargets)
    and a remote may be a URL or a path. Only plain names pass: no ``+ : ^ ~ ? * [ \\ @{``, no
    ``..``, and a remote has no ``/`` either.
    """
    if not name or name.startswith("-") or ".." in name:
        return False
    return bool((_PUSH_REMOTE_RE if remote else _PUSH_BRANCH_RE).match(name))


def _configured_remotes(repo_path: Path) -> set[str]:
    r = _git(["remote"], repo_path, check=False)
    return set(r.stdout.split()) if r.returncode == 0 else set()


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
    refuse_scan_source(repo_path)
    target_branch = branch or get_current_branch(repo_path)
    if not target_branch:
        return PushResult(
            pushed=False, remote=remote, branch="", remote_url="",
            skipped_reason="could not determine current branch",
        )
    # A value starting with "-" would reach git push as an option (--force, --mirror, --delete, ...).
    if not _valid_push_name(remote, remote=True):
        return PushResult(pushed=False, remote=remote, branch=target_branch, remote_url="",
                          skipped_reason=f"invalid remote name {remote!r}")
    if not _valid_push_name(target_branch) or _git(
            ["check-ref-format", "--branch", target_branch], repo_path, check=False).returncode != 0:
        return PushResult(pushed=False, remote=remote, branch=target_branch, remote_url="",
                          skipped_reason=f"invalid branch name {target_branch!r}")

    # Only a configured remote: never a URL or path named in its place.
    raw_url = get_remote_url(repo_path, remote) if remote in _configured_remotes(repo_path) else None
    # Remote URLs often embed tokens (https://x-access-token:...@host): never log or return them.
    remote_url = redact_secrets(raw_url) if raw_url is not None else None
    if remote_url is None:
        log.info("push: no remote %r in %s — skipping", remote, repo_path.name)
        return PushResult(
            pushed=False, remote=remote, branch=target_branch, remote_url="",
            skipped_reason=f"no remote {remote!r} configured",
        )

    if _is_local_path_remote(raw_url):
        log.info("push: remote %r is a local path — skipping (%s)", remote, remote_url)
        return PushResult(
            pushed=False, remote=remote, branch=target_branch, remote_url=remote_url,
            skipped_reason="remote is a local filesystem path — push skipped",
        )

    try:
        _git(
            ["push", "--set-upstream", remote,
             f"refs/heads/{target_branch}:refs/heads/{target_branch}"],
            repo_path,
            timeout=_PUSH_TIMEOUT,
        )
        log.info("pushed %r → %r (%s)", target_branch, remote, remote_url)
        return PushResult(pushed=True, remote=remote, branch=target_branch, remote_url=remote_url)
    except GitError as exc:
        reason = redact_secrets(str(exc))[:400]
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
        # Copy links as links: following them would pull host files (a link to
        # ~/.ssh or /) into the copy, and a dangling link would fail the copy.
        # git apply itself refuses to write beyond a symbolic link.
        shutil.copytree(str(repo_path), str(repo_copy), symlinks=True)

        patch_file = tmp_root / "changes.patch"
        patch_file.write_text(patch_text, encoding="utf-8")

        r = subprocess.run(
            ["git", "apply", "--whitespace=fix", str(patch_file)],
            cwd=repo_copy,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
