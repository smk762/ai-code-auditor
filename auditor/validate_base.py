"""The base tree ``validate_patch`` applies a patch to: the repo's ``dev`` tip, fetched on demand.

Mirrors are synced once a night, but packs are fixed during the day, often across several PRs,
so the mirror's ``origin/dev`` falls behind the code agents patch. Before each validation the
branch is fetched from the mirror's ``origin`` URL into a separate bare cache repo
(``<output_dir>/_validate_git/<name>-<hash>.git``) that borrows the mirror's objects through
``objects/info/alternates``. The mirror itself is never written: it is mounted read-only in the
audit-api container, and someone may be working in the checkout.

Fetches are bounded by a timeout and skipped when the cache fetched less than
``AI_AUDIT_VALIDATE_FETCH_TTL_S`` (default 60) seconds ago, so a burst of validations costs
one fetch. When a fetch fails, the mirror's own ``origin/dev`` (else ``HEAD``) is used and the
error is reported.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "LC_ALL": "C"}


@dataclass
class BaseRef:
    """Where the clean tree comes from. ``git_dir`` is ``None`` for a plain copy (not a git checkout)."""

    git_dir: Path | None
    ref: str          # what was resolved: "origin/dev", "HEAD" or "copy"
    sha: str = ""
    prefix: str = ""  # repo_path relative to the checkout top ("" at the top)
    source: str = "mirror"  # fetched | cached | mirror | copy
    fetch_error: str = ""

    @property
    def fresh(self) -> bool:
        return self.source in ("fetched", "cached")

    @property
    def treeish(self) -> str:
        return f"{self.sha}:{self.prefix.rstrip('/')}" if self.prefix else self.sha

    def describe(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("git_dir", None)
        d["fresh"] = self.fresh
        return d


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return default if not raw else raw not in ("0", "false", "no", "off")


def _git(*args: str, cwd: Path | None = None, git_dir: Path | None = None,
         timeout: float | None = 30) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    elif cwd is not None:
        cmd += ["-C", str(cwd)]
    return subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **_GIT_ENV})


def _verify(ref: str, *, cwd: Path | None = None, git_dir: Path | None = None) -> str:
    r = _git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", cwd=cwd, git_dir=git_dir)
    return r.stdout.strip() if r.returncode == 0 else ""


def _mirror_base(top: Path, prefix: str, branch: str, error: str = "", source: str = "mirror") -> BaseRef:
    for ref in (f"origin/{branch}", "HEAD"):
        sha = _verify(ref, cwd=top)
        if sha:
            return BaseRef(git_dir=Path(_git("rev-parse", "--absolute-git-dir", cwd=top).stdout.strip()),
                           ref=ref, sha=sha, prefix=prefix, source=source, fetch_error=error)
    raise RuntimeError(f"{top}: neither origin/{branch} nor HEAD resolves to a commit")


def cache_root(output_dir: str | Path | None = None) -> Path:
    explicit = (os.getenv("AI_AUDIT_VALIDATE_GIT_DIR") or "").strip()
    root = Path(explicit) if explicit else Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports") / "_validate_git"
    return root.resolve()


def resolve_base(repo_path: Path, *, branch: str = "dev", output_dir: str | Path | None = None,
                 fetch: bool | None = None) -> BaseRef:
    """The commit a patch for *repo_path* should be validated on, fetching *branch* first.

    Never writes to *repo_path*'s repository. *fetch* ``None`` reads ``AI_AUDIT_VALIDATE_FETCH``
    (default on)."""
    repo_path = Path(repo_path)
    top_r = _git("rev-parse", "--show-toplevel", cwd=repo_path)
    if top_r.returncode != 0:
        return BaseRef(git_dir=None, ref="copy", source="copy")
    top = Path(top_r.stdout.strip())
    prefix = _git("rev-parse", "--show-prefix", cwd=repo_path).stdout.strip()

    if not (_bool_env("AI_AUDIT_VALIDATE_FETCH", True) if fetch is None else fetch):
        return _mirror_base(top, prefix, branch, "fetch disabled")
    url = _git("config", "--get", "remote.origin.url", cwd=top).stdout.strip()
    if not url:
        return _mirror_base(top, prefix, branch, "mirror has no origin remote")
    objects = Path(_git("rev-parse", "--git-path", "objects", cwd=top).stdout.strip())
    objects = (top / objects).resolve() if not objects.is_absolute() else objects

    root = cache_root(output_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _mirror_base(top, prefix, branch, f"cache dir {root} unusable: {exc}")
    key = f"{top.name}-{hashlib.sha1(str(top).encode()).hexdigest()[:8]}"
    cache = root / f"{key}.git"
    stamp = root / f".{key}.fetched"
    ttl = float(os.getenv("AI_AUDIT_VALIDATE_FETCH_TTL_S") or 60)
    timeout = float(os.getenv("AI_AUDIT_VALIDATE_FETCH_TIMEOUT_S") or 20)
    remote_ref = f"refs/remotes/origin/{branch}"

    with open(root / f".{key}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one fetch per repo; concurrent validations wait and reuse it
        try:
            if not (cache / "HEAD").exists():
                r = _git("init", "--bare", "-q", str(cache))
                if r.returncode != 0:
                    return _mirror_base(top, prefix, branch, f"git init failed: {r.stderr.strip()[:200]}")
            (cache / "objects" / "info").mkdir(parents=True, exist_ok=True)
            (cache / "objects" / "info" / "alternates").write_text(f"{objects}\n", encoding="utf-8")

            sha = _verify(remote_ref, git_dir=cache)
            try:
                age = time.time() - stamp.stat().st_mtime
            except OSError:
                age = float("inf")
            if sha and age < ttl:
                return BaseRef(git_dir=cache, ref=f"origin/{branch}", sha=sha, prefix=prefix, source="cached")

            try:
                r = _git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", url,
                         f"+refs/heads/{branch}:{remote_ref}", git_dir=cache, timeout=timeout)
                err = "" if r.returncode == 0 else (r.stderr.strip() or f"git fetch exited {r.returncode}")[:300]
            except subprocess.TimeoutExpired:
                err = f"git fetch timed out after {timeout:g}s"
            sha = _verify(remote_ref, git_dir=cache) if not err else ""
            if sha:
                stamp.touch()
                return BaseRef(git_dir=cache, ref=f"origin/{branch}", sha=sha, prefix=prefix, source="fetched")
            err = err or f"{branch} not found on origin"
        except OSError as exc:
            err = f"cache repo unusable: {exc}"
    logger.warning("validate_patch: could not fetch %s of %s, using the mirror: %s", branch, top.name, err)
    return _mirror_base(top, prefix, branch, err)


def staleness(base: BaseRef, repo: str, *, state_path: Path | None = None) -> dict[str, Any]:
    """Compare *base* with the ``dev`` tip merge-poll last saw on GitHub for *repo*.

    ``base_stale`` is True when that tip is not contained in the base (a commit that isn't even
    known locally counts), ``None`` when merge-poll has no tip for the repo. ``behind_by`` is the
    commit count when the tip is known locally."""
    from auditor.merge_poll import load_state, merge_poll_state_path

    if base.git_dir is None or not base.sha:
        return {"base_stale": None}
    tip = str(((load_state(state_path or merge_poll_state_path()).get("repos") or {}).get(repo) or {}).get("sha") or "")
    if not tip:
        return {"base_stale": None}
    if tip == base.sha or _git("merge-base", "--is-ancestor", tip, base.sha, git_dir=base.git_dir).returncode == 0:
        return {"base_stale": False, "dev_tip": tip}
    out: dict[str, Any] = {"base_stale": True, "dev_tip": tip}
    r = _git("rev-list", "--count", f"{base.sha}..{tip}", git_dir=base.git_dir)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        out["behind_by"] = int(r.stdout.strip())
    return out


def export_tree(base: BaseRef, repo_path: Path, dest: Path) -> None:
    """Write *base*'s tree (the *repo_path* subtree) to *dest*; a plain copy when not a git checkout."""
    import shutil
    import tarfile

    if base.git_dir is None:
        # Do not preserve symlinks: a crafted patch must not write through a
        # repo symlink into the host filesystem outside this temp tree.
        shutil.copytree(str(repo_path), str(dest), symlinks=False)
        return
    dest.mkdir(parents=True)
    proc = subprocess.Popen(["git", "--git-dir", str(base.git_dir), "archive", "--format=tar", base.treeish],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, **_GIT_ENV})
    assert proc.stdout is not None and proc.stderr is not None
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        tar.extractall(dest, filter="data")  # "data" refuses absolute paths, .. and links out of dest
    if proc.wait() != 0:
        raise RuntimeError(f"git archive {base.ref} failed: {proc.stderr.read().decode(errors='replace')[:300]}")


__all__ = ["BaseRef", "cache_root", "export_tree", "resolve_base", "staleness"]
