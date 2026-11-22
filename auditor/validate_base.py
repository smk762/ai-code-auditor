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

A private repo (argus-iris, voluptas) needs credentials. On the host the mirror sync gets them from
``gh auth git-credential``, but the audit-api container has no credential helper, only
``GITHUB_TOKEN`` (or ``GH_TOKEN`` / ``AI_AUDIT_GITHUB_TOKEN``) from ``.env``. So a fetch from an
``https://github.com/`` URL offers that token through an askpass script (never in the URL or the
command line), and only to github.com: a mirror's remote pointing elsewhere is fetched without it.

A request may pin an older base (``base_ref``, #91) when its fix has already merged and no longer
applies to the tip: :func:`pin_base` resolves it and refuses anything that isn't an ancestor of the ``dev`` tip, so evidence can't come
from an unrelated branch.

The exported tree is its own git checkout (#97): :func:`attach_head` gives it a ``HEAD`` at the
base commit, borrowing objects through alternates, and the tags on its history, because tests
that read git (``rev-parse HEAD``, ``describe --tags``) otherwise error on both trees and are
dropped as known failures. The patch then shows as working-tree changes.
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
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "LC_ALL": "C"}


@dataclass
class BaseRef:
    """Where the clean tree comes from. ``git_dir`` is ``None`` for a plain copy (not a git checkout)."""

    git_dir: Path | None
    ref: str          # what was resolved: "origin/dev", "HEAD", "copy", or a pinned base_ref
    sha: str = ""
    prefix: str = ""  # repo_path relative to the checkout top ("" at the top)
    source: str = "mirror"  # fetched | cached | mirror | copy | pinned
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


def _float_env(name: str, default: float) -> float:
    """A non-negative number from *name*; unset, malformed or negative reads as *default*."""
    try:
        value = float((os.getenv(name) or "").strip() or default)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, os.getenv(name), default)
        return default
    return value if value >= 0 and value != float("inf") else default


def _fetchable_url(url: str, top: Path) -> str:
    """*url* as ``git fetch`` should see it from the cache repo: a relative local path is
    resolved against the mirror's top, where its ``remote.origin.url`` is meant. URLs with a
    scheme, scp-style ``host:path`` and absolute paths are left alone."""
    if "://" in url or os.path.isabs(url):
        return url
    colon, slash = url.find(":"), url.find("/")
    if colon > 0 and (slash < 0 or colon < slash):  # git's scp-like syntax
        return url
    return str((top / url).resolve())


def _git(*args: str, cwd: Path | None = None, git_dir: Path | None = None,
         timeout: float | None = 30, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    elif cwd is not None:
        cmd += ["-C", str(cwd)]
    return subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **_GIT_ENV, **(env or {})})


def _is_github_https(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme == "https" and host == "github.com"


def _sanitize_git_output(text: str, token: str) -> str:
    from auditor.repo_resolver import _sanitize_git_output as sanitize

    return sanitize(text, token)


def _fetch_auth(url: str) -> tuple[dict[str, str], str]:
    """``(extra env, token)`` for fetching *url*: the GitHub token through an askpass script for an
    ``https://github.com/`` URL when one is set, else nothing (a public repo needs none)."""
    from auditor.merge_poll import github_token
    from auditor.repo_resolver import _askpass_script  # also used by the remote-repo cache clones

    token = github_token()
    if not token or not _is_github_https(url):
        return {}, ""
    return {"GIT_ASKPASS": _askpass_script(), "GIT_TOKEN": token}, token


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
    ttl = _float_env("AI_AUDIT_VALIDATE_FETCH_TTL_S", 60)
    timeout = _float_env("AI_AUDIT_VALIDATE_FETCH_TIMEOUT_S", 20) or 20  # 0 would mean no bound
    remote_ref = f"refs/remotes/origin/{branch}"

    try:
        lock = open(root / f".{key}.lock", "w")  # noqa: SIM115 — closed by the with below
    except OSError as exc:  # an existing but unwritable cache dir (read-only, wrong owner, full)
        return _mirror_base(top, prefix, branch, f"cache dir {root} unusable: {exc}")
    with lock:
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

            fetch_url = _fetchable_url(url, top)
            auth_env, token = _fetch_auth(fetch_url)
            try:
                # tags too: tests may `git describe` the tree (attach_head copies those on the base)
                r = _git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", fetch_url,
                         f"+refs/heads/{branch}:{remote_ref}", "+refs/tags/*:refs/tags/*",
                         git_dir=cache, timeout=timeout, env=auth_env)
                err = "" if r.returncode == 0 else _sanitize_git_output(
                    r.stderr.strip() or f"git fetch exited {r.returncode}", token)[:300]
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


class BaseRefError(ValueError):
    """A requested ``base_ref`` that can't be used; the message is safe to return to the caller."""



def pin_base(tip: BaseRef, base_ref: str, repo_path: Path) -> BaseRef:
    """*tip* moved back to *base_ref*, which must be an ancestor of (or equal to) ``tip.sha``.

    *base_ref* is resolved in the cache repo, then among the mirror's own refs. Nothing is
    fetched: an ancestor of the tip is already present wherever the tip is, so a sha that isn't
    there couldn't pass the ancestry check anyway. Raises :class:`BaseRefError` when it is
    unsafe, unknown, or not on ``dev``."""
    from auditor.diff_auditor import git_ref_arg

    try:
        git_ref_arg(base_ref)
    except RuntimeError as exc:
        raise BaseRefError(str(exc)) from exc
    if tip.git_dir is None or not tip.sha:
        raise BaseRefError("base_ref needs a git mirror; this repo is a plain copy")
    top_r = _git("rev-parse", "--show-toplevel", cwd=Path(repo_path))
    top = Path(top_r.stdout.strip()) if top_r.returncode == 0 else None

    sha = _verify(base_ref, git_dir=tip.git_dir) or (_verify(base_ref, cwd=top) if top else "")
    if not sha:
        raise BaseRefError(f"base_ref {base_ref!r} is not a commit in this repo")
    if sha != tip.sha and _git("merge-base", "--is-ancestor", sha, tip.sha, git_dir=tip.git_dir).returncode != 0:
        raise BaseRefError(f"base_ref {base_ref!r} ({sha[:12]}) is not an ancestor of {tip.ref} ({tip.sha[:12]})")
    return BaseRef(git_dir=tip.git_dir, ref=base_ref, sha=sha, prefix=tip.prefix, source="pinned",
                   fetch_error=tip.fetch_error)


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
        # Links are copied as links, never followed (a link to ~/.ssh or / would copy host files into
        # the tree, or recurse forever), then every link that leads out of the tree is removed, so a
        # crafted patch can't write through one into the host filesystem either.
        shutil.copytree(str(repo_path), str(dest), symlinks=True)
        _drop_escaping_links(dest)
        return
    dest.mkdir(parents=True)
    import tempfile

    # stderr to a file: a pipe read only after stdout's EOF could fill and block git
    with tempfile.TemporaryFile() as err, subprocess.Popen(
            ["git", "--git-dir", str(base.git_dir), "archive", "--format=tar", base.treeish],
            stdout=subprocess.PIPE, stderr=err, env={**os.environ, **_GIT_ENV}) as proc:
        assert proc.stdout is not None
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                tar.extractall(dest, filter="data")  # "data" refuses absolute paths, .. and links out of dest
        except (tarfile.TarError, OSError):
            proc.kill()
            if proc.wait() != 0:  # git's own error explains an empty or cut-off stream
                err.seek(0)
                raise RuntimeError(f"git archive {base.ref} failed: "
                                   f"{err.read().decode(errors='replace').strip()[:300]}") from None
            raise
        if proc.wait() != 0:
            err.seek(0)
            raise RuntimeError(f"git archive {base.ref} failed: {err.read().decode(errors='replace').strip()[:300]}")
    attach_head(base, dest)


def _drop_escaping_links(dest: Path) -> None:
    """Remove every symlink under *dest* whose target resolves outside it (or can't be resolved)."""
    root = dest.resolve()
    for dirpath, dirnames, filenames in os.walk(dest):  # doesn't descend into linked dirs
        for name in [*dirnames, *filenames]:
            p = Path(dirpath) / name
            if not p.is_symlink():
                continue
            try:
                inside = p.resolve().is_relative_to(root)
            except (OSError, RuntimeError):  # a link loop
                inside = False
            if not inside:
                p.unlink()


_HEAD_COMMIT_ENV = {"GIT_AUTHOR_NAME": "validate_patch", "GIT_AUTHOR_EMAIL": "validate_patch@invalid",
                    "GIT_COMMITTER_NAME": "validate_patch", "GIT_COMMITTER_EMAIL": "validate_patch@invalid",
                    "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000"}


def _copy_tags(base: BaseRef, dest: Path) -> None:
    """Give *dest* the tags of *base*'s repository that point into ``base.sha``'s history, so
    ``git describe`` there sees what a CI checkout would. Best effort: a tree without tags is
    still a usable tree."""
    try:
        r = _git("for-each-ref", "--merged", base.sha, "--format=%(objectname) %(refname)", "refs/tags",
                 git_dir=base.git_dir)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("validate_patch: tags not listed for %s: %s", dest, exc)
        return
    lines = [ln for ln in r.stdout.splitlines() if ln.count(" ") == 1] if r.returncode == 0 else []
    if not lines:
        return
    stdin = "".join(f"create {ref} {sha}\n" for sha, ref in (ln.split(" ") for ln in lines))
    try:
        w = subprocess.run(["git", "-C", str(dest), "update-ref", "--stdin"], input=stdin, capture_output=True,
                           text=True, timeout=30, env={**os.environ, **_GIT_ENV}, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:  # best effort: the tree keeps its HEAD
        logger.warning("validate_patch: tags not copied into %s: %s", dest, exc)
        return
    if w.returncode != 0:
        logger.warning("validate_patch: tags not copied into %s: %s", dest, w.stderr.strip()[:200])


def attach_head(base: BaseRef, dest: Path) -> str:
    """Make the exported *dest* a git checkout whose ``HEAD`` is *base*'s commit (#97).

    Tests that read history (``git rev-parse HEAD``, a docs build stamping pages with their
    commit) error in a bare ``git archive`` export. *dest* gets its own repository that borrows
    *base*'s objects through ``objects/info/alternates``, so nothing is copied and *base*'s
    repository is only read. The index is filled from ``HEAD``, so ``git status`` shows the patch,
    and the tags in ``HEAD``'s history are copied, so ``git describe`` works.

    For a subtree export (a repo inside a monorepo), ``HEAD`` is a commit on top of ``base.sha``
    whose tree is that subtree, so the layout matches and the history is still reachable.
    Returns the ``HEAD`` sha, or "" when git couldn't set it up (the tree is still usable)."""
    if base.git_dir is None or not base.sha:
        return ""
    try:
        objects = _git("rev-parse", "--git-path", "objects", git_dir=base.git_dir).stdout.strip()
        objects_dir = Path(objects) if Path(objects).is_absolute() else (base.git_dir / objects)
        r = _git("init", "-q", "-b", "dev", str(dest))
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:200])
        (dest / ".git" / "objects" / "info" / "alternates").write_text(f"{objects_dir.resolve()}\n", encoding="utf-8")
        head = base.sha
        if base.prefix:
            env = {**os.environ, **_GIT_ENV, **_HEAD_COMMIT_ENV}
            c = subprocess.run(["git", "-C", str(dest), "commit-tree", base.treeish, "-p", base.sha, "-m",
                                f"validate_patch: {base.prefix.rstrip('/')} of {base.sha[:12]}"],
                               capture_output=True, text=True, timeout=30, env=env, check=False)
            if c.returncode != 0:
                raise RuntimeError(c.stderr.strip()[:200])
            head = c.stdout.strip()
        for args in (("update-ref", "refs/heads/dev", head), ("read-tree", head)):
            r = _git(*args, cwd=dest)
            if r.returncode != 0:
                raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[:200]}")
        _copy_tags(base, dest)
        return head
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        import shutil

        logger.warning("validate_patch: %s has no git HEAD, tests that read history will error: %s", dest, exc)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return ""


__all__ = ["BaseRef", "BaseRefError", "attach_head", "cache_root", "export_tree", "pin_base", "resolve_base", "staleness"]
