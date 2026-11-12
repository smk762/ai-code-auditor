"""Hourly GitHub API poll for ``dev`` tip changes → merge-refresh queue.

Outbound-only: no inbound webhook / LAN exposure required. Compares each
configured mirror's remote branch tip to a local state file and enqueues a
lightweight merge refresh when the SHA moves.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from auditor.config import _bool_env

logger = logging.getLogger(__name__)

_GITHUB_OWNER_REPO = re.compile(
    r"(?:github\.com[:/]|git@github\.com:)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def merge_poll_enabled() -> bool:
    return _bool_env("AI_AUDIT_MERGE_POLL_ENABLED", True)


def merge_poll_state_path(output_dir: str | Path | None = None) -> Path:
    explicit = (os.getenv("AI_AUDIT_MERGE_POLL_STATE") or "").strip()
    if explicit:
        return Path(explicit)
    root = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    return root / "merge_poll_state.json"


def github_token() -> str:
    return (
        os.getenv("GITHUB_TOKEN")
        or os.getenv("GH_TOKEN")
        or os.getenv("AI_AUDIT_GITHUB_TOKEN")
        or ""
    ).strip()


def parse_github_slug(url: str) -> str | None:
    """Return ``owner/repo`` from a GitHub remote URL, or None."""
    raw = (url or "").strip()
    if not raw:
        return None
    m = _GITHUB_OWNER_REPO.search(raw)
    if m:
        return f"{m.group('owner')}/{m.group('repo')}"
    # Fallback: path-style https://github.com/owner/repo
    try:
        parsed = urlparse(raw)
        if "github.com" in (parsed.netloc or "") and parsed.path:
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    except Exception:
        return None
    return None


@dataclass
class RepoTip:
    name: str
    slug: str
    sha: str = ""
    error: str = ""


@dataclass
class PollResult:
    polled_at: str
    branch: str
    checked: int = 0
    changed: list[str] = field(default_factory=list)
    changes: list[dict[str, str]] = field(default_factory=list)  # repo/before/after
    unchanged: list[str] = field(default_factory=list)
    baselined: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    queued: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"repos": {}, "updated_at": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"repos": {}, "updated_at": ""}
    if not isinstance(data, dict):
        return {"repos": {}, "updated_at": ""}
    repos = data.get("repos")
    if not isinstance(repos, dict):
        data["repos"] = {}
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _github_headers(token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-code-auditor-merge-poll",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_branch_tip(slug: str, branch: str, *, token: str = "", timeout_s: float = 20.0) -> str:
    """Return the commit SHA at ``heads/<branch>`` via the GitHub REST API.

    If *branch* is missing (404), tries ``main`` then ``master``. A 404 with no
    token usually means a private repo — set ``GITHUB_TOKEN``.
    """
    # Without a token, do not probe main/master after a 404 — private repos
    # always 404 anonymously and would burn the 60 req/hr IP budget.
    candidates = [branch]
    if token:
        for alt in ("main", "master"):
            if alt not in candidates:
                candidates.append(alt)

    last_err = ""
    for cand in candidates:
        url = f"https://api.github.com/repos/{slug}/git/ref/heads/{cand}"
        req = urllib.request.Request(url, headers=_github_headers(token), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            last_err = f"GitHub {exc.code} for {slug}@{cand}: {body}"
            if exc.code == 404:
                continue
            raise RuntimeError(last_err) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub unreachable for {slug}: {exc}") from exc

        obj = payload.get("object") if isinstance(payload, dict) else None
        sha = str((obj or {}).get("sha") or "").strip() if isinstance(obj, dict) else ""
        if sha:
            return sha
        last_err = f"No SHA in GitHub ref response for {slug}@{cand}"

    hint = ""
    if not token and "404" in last_err:
        hint = " (set GITHUB_TOKEN if the repo is private)"
    raise RuntimeError((last_err or f"no tip for {slug}") + hint)


def list_poll_targets(
    mirrors_path: str | Path = "config/repo_mirrors.yaml",
) -> list[tuple[str, str]]:
    """Return ``[(name, owner/repo), ...]`` from mirrors config."""
    from conductor.mirrors import load_mirrors_config

    cfg = load_mirrors_config(mirrors_path)
    out: list[tuple[str, str]] = []
    for item in cfg.get("mirrors") or []:
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            continue
        slug = parse_github_slug(url)
        if not slug:
            logger.warning("skip %s: not a GitHub URL (%s)", name, url)
            continue
        out.append((name, slug))
    return out


def poll_dev_tips(
    *,
    branch: str | None = None,
    mirrors_path: str | Path = "config/repo_mirrors.yaml",
    state_path: Path | None = None,
    enqueue: bool = True,
    baseline_only: bool = False,
    force_queue: bool = True,
    token: str | None = None,
    dispatch: Callable[[Any], dict[str, Any]] | None = None,
) -> PollResult:
    """Poll each mirror's remote branch tip; enqueue merge refresh on SHA change.

    First sighting of a repo only baselines the SHA (no refresh) unless
    ``baseline_only`` is already implied — avoids a stampede on first install.

    *dispatch* receives each :class:`MergeEvent` (default: the in-process
    scheduler's ``enqueue``). When dispatch raises or does not accept the
    event, the previous SHA is kept in state so the change is retried on the
    next poll instead of being silently dropped.
    """
    from auditor.merge_hook import MergeEvent, get_scheduler, merge_branch, merge_hook_enabled

    br = (branch or merge_branch()).strip() or "dev"
    path = state_path or merge_poll_state_path()
    state = load_state(path)
    repos_state: dict[str, Any] = dict(state.get("repos") or {})
    tok = token if token is not None else github_token()

    result = PollResult(
        polled_at=datetime.now(timezone.utc).isoformat(),
        branch=br,
    )

    if not merge_poll_enabled():
        result.errors.append({"repo": "*", "error": "merge poll disabled"})
        return result

    targets = list_poll_targets(mirrors_path)
    for name, slug in targets:
        result.checked += 1
        try:
            tip = fetch_branch_tip(slug, br, token=tok)
        except Exception as exc:
            logger.warning("poll failed for %s (%s): %s", name, slug, exc)
            result.errors.append({"repo": name, "error": str(exc)[:400]})
            time.sleep(0.05)
            continue

        prev = repos_state.get(name) or {}
        prev_sha = str(prev.get("sha") or "").strip()
        recorded_sha = tip

        if not prev_sha:
            result.baselined.append(name)
        elif prev_sha == tip:
            result.unchanged.append(name)
        else:
            result.changed.append(name)
            result.changes.append({"repo": name, "before": prev_sha, "after": tip})
            if not baseline_only and enqueue:
                if dispatch is None and not merge_hook_enabled():
                    result.errors.append(
                        {"repo": name, "error": "merge hook disabled; not queued"}
                    )
                    recorded_sha = prev_sha
                else:
                    send = dispatch or get_scheduler().enqueue
                    event = MergeEvent(
                        repo=name,
                        before=prev_sha,
                        after=tip,
                        ref=f"refs/heads/{br}",
                        source="poll",
                        force=force_queue,
                    )
                    try:
                        queued = send(event)
                    except Exception as exc:
                        logger.warning("dispatch failed for %s: %s", name, exc)
                        result.errors.append({"repo": name, "error": str(exc)[:400]})
                        recorded_sha = prev_sha
                    else:
                        result.queued.append(queued)
                        if not queued.get("accepted"):
                            recorded_sha = prev_sha

        repos_state[name] = {
            "slug": slug,
            "sha": recorded_sha,
            "branch": br,
            "checked_at": result.polled_at,
        }
        time.sleep(0.05)

    state["repos"] = repos_state
    state["branch"] = br
    save_state(path, state)
    logger.info(
        "merge poll: checked=%d changed=%d baselined=%d errors=%d queued=%d",
        result.checked,
        len(result.changed),
        len(result.baselined),
        len(result.errors),
        len(result.queued),
    )
    return result
