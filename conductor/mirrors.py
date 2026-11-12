"""Sync local mirrors and check out the scan branch (default: dev) before rest sessions."""

from __future__ import annotations

import logging
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


def checkout_scan_branch(repo: Path, preferred: str = "dev") -> str:
    """Fetch + checkout preferred branch (dev), falling back to main/master."""
    _git(["fetch", "--all", "--prune"], repo)
    candidates = [preferred, "main", "master"]
    for name in candidates:
        local = name
        remote = f"origin/{name}"
        if _branch_exists(repo, local) or _branch_exists(repo, remote):
            if _branch_exists(repo, remote):
                # Create/update local tracking branch from origin without
                # destroying divergent local commits (never reset --hard).
                if _branch_exists(repo, local):
                    _git(["checkout", local], repo)
                    ff = _git(["merge", "--ff-only", remote], repo)
                    if ff.returncode != 0:
                        logger.warning(
                            "ff-only update failed for %s (%s → %s); leaving local branch as-is: %s",
                            repo.name,
                            local,
                            remote,
                            (ff.stderr or ff.stdout or "diverged")[:200],
                        )
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
            checked = checkout_scan_branch(path, preferred)
            return MirrorSyncResult(
                name=name, path=str(path), ok=True, branch=checked, action="fetch"
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
    return results
