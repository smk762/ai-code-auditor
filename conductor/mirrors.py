"""Sync local mirrors and check out the scan branch (default: dev) before rest sessions."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MirrorSyncResult:
    name: str
    path: str
    ok: bool
    branch: str = ""
    action: str = ""
    error: str = ""


def _git(args: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _branch_exists(repo: Path, ref: str) -> bool:
    r = _git(["rev-parse", "--verify", "--quiet", ref], repo)
    return r.returncode == 0


def _local_only_commits(repo: Path, local: str, remote: str) -> list[str]:
    """Non-merge commits on *local* with no patch-equivalent on *remote* (``git cherry`` "+" lines).

    Raises RuntimeError when git can't answer, so a failed check never reads as "nothing unique"."""
    r = _git(["cherry", remote, local], repo)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "git cherry failed").strip()[:200])
    return [ln[2:] for ln in r.stdout.splitlines() if ln.startswith("+ ")]


def _contained_upstream(repo: Path, local: str, remote: str) -> bool:
    """True when merging *local* into *remote* would change nothing (catches squashed rewrites,
    which ``git cherry`` can't match commit by commit). Conflicts or old git mean False."""
    r = _git(["merge-tree", "--write-tree", remote, local], repo)
    if r.returncode != 0:
        return False
    merged = (r.stdout.splitlines() or [""])[0].strip()
    return bool(merged) and merged == _git(["rev-parse", f"{remote}^{{tree}}"], repo).stdout.strip()


def _update_local_branch(repo: Path, local: str, remote: str) -> str:
    """Bring checked-out *local* up to *remote*. Returns ``ff`` / ``reset`` / ``diverged`` /
    ``checkout-failed``.

    A fast-forward is tried first. If the branches diverged and every local commit already exists
    upstream (an upstream history rewrite: ``git cherry`` matches rebased commits, and a merge that
    would change nothing covers squashes), the local branch is
    reset to *remote*: its old tip is kept on ``backup/pre-sync-<sha>`` and uncommitted changes are
    stashed, so nothing is lost. If any local commit is unique, the branch is left as-is."""
    if _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip() != local:
        logger.warning("%s: could not check out %s; leaving it as-is", repo.name, local)
        return "checkout-failed"
    ff = _git(["merge", "--ff-only", remote], repo)
    if ff.returncode == 0:
        return "ff"
    try:
        unique = _local_only_commits(repo, local, remote)
    except RuntimeError as exc:
        logger.warning("could not compare %s %s with %s; leaving it as-is: %s", repo.name, local, remote, exc)
        return "diverged"
    if unique and not _contained_upstream(repo, local, remote):
        logger.warning(
            "%s: %s has %d commit(s) not on %s (e.g. %s); leaving local branch as-is",
            repo.name, local, len(unique), remote, unique[0][:12],
        )
        return "diverged"
    old = _git(["rev-parse", "--short=12", local], repo).stdout.strip()
    backup = f"backup/pre-sync-{old}"
    if _git(["branch", "-f", backup, local], repo).returncode != 0:
        logger.warning("%s: could not create %s; not resetting %s", repo.name, backup, local)
        return "diverged"
    if _git(["status", "--porcelain"], repo).stdout.strip():
        st = _git(["stash", "push", "--include-untracked", "-m", f"mirror sync: before reset of {local} ({old})"], repo)
        if st.returncode != 0:
            logger.warning("%s: could not stash local changes; not resetting %s", repo.name, local)
            return "diverged"
    _git(["reset", "--hard", remote], repo, check=True)
    logger.warning(
        "%s: %s was rewritten upstream (all local commits already on %s); reset to it, old tip kept on %s",
        repo.name, local, remote, backup,
    )
    return "reset"


def checkout_scan_branch(repo: Path, preferred: str = "dev", report: dict[str, str] | None = None) -> str:
    """Fetch + checkout preferred branch (dev), falling back to main/master.

    An existing local branch is updated with :func:`_update_local_branch`; its outcome goes in
    ``report["update"]`` when *report* is given."""
    _git(["fetch", "--all", "--prune"], repo)
    candidates = [preferred, "main", "master"]
    for name in candidates:
        local = name
        remote = f"origin/{name}"
        if _branch_exists(repo, local) or _branch_exists(repo, remote):
            if _branch_exists(repo, remote):
                if _branch_exists(repo, local):
                    _git(["checkout", local], repo)
                    outcome = _update_local_branch(repo, local, remote)
                    if report is not None:
                        report["update"] = outcome
                else:
                    _git(["checkout", "-B", local, remote], repo)
            else:
                _git(["checkout", local], repo)
            return name
    # Stay on current HEAD if nothing matches
    cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    return (cur.stdout or "HEAD").strip()


def load_mirrors_config(path: str | Path = "config/repo_mirrors.yaml") -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _sync_mirror_item(
    item: dict[str, Any],
    *,
    preferred: str,
) -> MirrorSyncResult:
    """Clone/fetch one mirrors.yaml entry and check out *preferred*."""
    name = str(item.get("name") or "")
    path = Path(item.get("path") or "")
    url = str(item.get("url") or "").strip()
    if not path:
        return MirrorSyncResult(name=name, path="", ok=False, action="skip", error="empty path")
    try:
        if path.exists() and (path / ".git").exists():
            rep: dict[str, str] = {}
            checked = checkout_scan_branch(path, preferred, report=rep)
            update = rep.get("update", "")
            return MirrorSyncResult(
                name=name, path=str(path), ok=True, branch=checked,
                action=f"fetch+{update}" if update else "fetch",
                error={"diverged": "local commits not upstream; left as-is",
                       "checkout-failed": f"could not check out {checked}; left as-is"}.get(update, ""),
            )
        if url:
            path.parent.mkdir(parents=True, exist_ok=True)
            clone = subprocess.run(
                ["git", "clone", "--branch", preferred, url, str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if clone.returncode != 0:
                # Preferred branch may not exist yet — clone default then checkout
                subprocess.run(
                    ["git", "clone", url, str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if path.exists() and (path / ".git").exists():
                checked = checkout_scan_branch(path, preferred)
                return MirrorSyncResult(
                    name=name, path=str(path), ok=True, branch=checked, action="clone"
                )
            return MirrorSyncResult(
                name=name,
                path=str(path),
                ok=False,
                action="clone",
                error=(clone.stderr or "clone failed")[:300],
            )
        return MirrorSyncResult(
            name=name,
            path=str(path),
            ok=False,
            action="skip",
            error="no url and path missing",
        )
    except Exception as exc:
        logger.exception("mirror sync failed for %s", name)
        return MirrorSyncResult(name=name, path=str(path), ok=False, action="error", error=str(exc))


def ensure_checkout_aliases(cfg: dict[str, Any]) -> list[MirrorSyncResult]:
    """Symlink each ``aliases`` entry to its mirror in every directory that holds a mirror.

    Idempotent. A stale symlink at the alias path is repointed; a real file or directory there
    is never touched (reported, not failed).
    """
    aliases = cfg.get("aliases") or {}
    if not aliases:
        return []
    items = [i for i in cfg.get("mirrors") or [] if i.get("path")]
    paths = {str(i.get("name") or ""): Path(i["path"]) for i in items}
    parents = sorted({p.parent for p in paths.values()} | (
        {Path(cfg["mirror_root"])} if cfg.get("mirror_root") else set()
    ))
    results: list[MirrorSyncResult] = []
    for alias, target_name in aliases.items():
        target = paths.get(str(target_name))
        name = f"alias:{alias}"
        if target is None:
            results.append(MirrorSyncResult(
                name=name, path="", ok=False, action="alias",
                error=f"alias target {target_name!r} is not a configured mirror",
            ))
            continue
        for parent in parents:
            link = parent / str(alias)
            if link == target or not parent.is_dir():
                continue
            rel = os.path.relpath(target, parent)
            try:
                if link.is_symlink():
                    if os.readlink(link) == rel:
                        results.append(MirrorSyncResult(name=name, path=str(link), ok=True, action="alias-ok"))
                        continue
                    link.unlink()
                elif link.exists():
                    results.append(MirrorSyncResult(
                        name=name, path=str(link), ok=True, action="alias-kept-existing",
                        error="a real file/dir is there; left alone",
                    ))
                    continue
                link.symlink_to(rel, target_is_directory=True)
                results.append(MirrorSyncResult(name=name, path=str(link), ok=True, action=f"alias->{rel}"))
            except OSError as exc:
                results.append(MirrorSyncResult(
                    name=name, path=str(link), ok=False, action="alias", error=str(exc)[:300],
                ))
    return results


def sync_one_mirror(
    name: str,
    config_path: str | Path = "config/repo_mirrors.yaml",
    *,
    branch: str | None = None,
) -> MirrorSyncResult:
    """Clone/fetch a single named mirror and check out *branch*.

    An explicit *branch* wins; otherwise the config ``default_branch`` (or ``dev``).
    """
    cfg = load_mirrors_config(config_path)
    root = Path(cfg.get("mirror_root") or "/home/smk/mirrors")
    root.mkdir(parents=True, exist_ok=True)
    preferred = str(branch or cfg.get("default_branch") or "dev")
    needle = name.strip().lower()
    for item in cfg.get("mirrors") or []:
        item_name = str(item.get("name") or "")
        if item_name.lower() == needle:
            return _sync_mirror_item(item, preferred=preferred)
    return MirrorSyncResult(
        name=name,
        path="",
        ok=False,
        action="skip",
        error=f"mirror {name!r} not found in {config_path}",
    )


def sync_mirrors(
    config_path: str | Path = "config/repo_mirrors.yaml",
    *,
    branch: str = "dev",
) -> list[MirrorSyncResult]:
    """Clone/fetch every configured mirror and check out *branch* (default dev)."""
    cfg = load_mirrors_config(config_path)
    root = Path(cfg.get("mirror_root") or "/home/smk/mirrors")
    root.mkdir(parents=True, exist_ok=True)
    preferred = str(cfg.get("default_branch") or branch)
    results: list[MirrorSyncResult] = []

    for item in cfg.get("mirrors") or []:
        results.append(_sync_mirror_item(item, preferred=preferred))
    results.extend(ensure_checkout_aliases(cfg))
    return results
