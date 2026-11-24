"""Where a repo's files are: its mirror on this host, and a finding's path inside it.

It imports nothing else from ``auditor`` so anything can use it. These lived in ``trust_metrics``,
which tied packs and the suite rules into one import cycle with everything trust metrics reads (#154).
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def rel_path(repo: str, path: str) -> str:
    """Repo-relative path. Stored paths are absolute and differ by checkout (mirror or home)."""
    p = str(path or "")
    marker = f"/{repo}/"
    if marker in p:
        # The first occurrence is the checkout root; a same-named subdirectory must survive.
        return p.split(marker, 1)[1]
    # Strip a literal "./" prefix only: lstrip("./") would eat the dot of ".github/" or ".env".
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def anchored(repo: str, path: str) -> bool:
    """Whether ``rel_path`` can be sure where the repo root is in *path* (#171).

    Not when an absolute path never names the repo, the name comes up more than once, or a relative
    path starts with the repo's name (a checkout, or a package named after the repo)."""
    p = str(path or "")
    if p.startswith("/"):
        return p.split("/")[:-1].count(repo) == 1
    while p.startswith("./"):
        p = p[2:]
    return repo not in p.split("/")[:-1]


def mirror_paths(mirrors_path: str | Path = "config/repo_mirrors.yaml") -> dict[str, Path]:
    try:
        from conductor.mirrors import load_mirrors_config

        cfg = load_mirrors_config(mirrors_path)
    except Exception as exc:  # noqa: BLE001
        logger.info("no mirrors config: %s", exc)
        return {}
    return {str(m["name"]): Path(m["path"]) for m in cfg.get("mirrors") or [] if m.get("name") and m.get("path")}
