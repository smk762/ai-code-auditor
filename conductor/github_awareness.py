"""Open PR / issue awareness so night conductor avoids clashing or duplicating work."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OpenItem:
    kind: str  # pr | issue
    number: int
    title: str
    url: str
    body: str = ""
    files: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def haystack(self) -> str:
        parts = [self.title, self.body, " ".join(self.files), " ".join(self.labels)]
        return " ".join(parts).lower()


@dataclass(slots=True)
class RepoAwareness:
    repo: str
    items: list[OpenItem] = field(default_factory=list)
    error: str = ""

    def brief(self, limit: int = 20) -> str:
        if self.error:
            return f"(awareness unavailable: {self.error})"
        if not self.items:
            return "(no open PRs or issues)"
        lines = []
        for it in self.items[:limit]:
            files = f" files={','.join(Path(f).name for f in it.files[:5])}" if it.files else ""
            lines.append(f"- {it.kind.upper()}#{it.number}: {it.title}{files}")
        return "\n".join(lines)


def _gh_json(args: list[str], cwd: Path | None = None) -> Any:
    r = subprocess.run(
        ["gh", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "gh failed")[:400])
    if not (r.stdout or "").strip():
        return []
    return json.loads(r.stdout)


def fetch_repo_awareness(
    repo_path: Path,
    *,
    repo_slug: str | None = None,
) -> RepoAwareness:
    """Load open PRs (with files) and open issues for a local checkout via gh."""
    name = repo_slug or repo_path.name
    if not (repo_path / ".git").exists():
        return RepoAwareness(repo=name, error="not a git repo")
    try:
        # Prefer remote-derived owner/name for gh
        repo_arg: list[str] = []
        if repo_slug and "/" in repo_slug:
            repo_arg = ["--repo", repo_slug]
        else:
            # Let gh resolve from git remotes in cwd
            repo_arg = []

        prs = _gh_json(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "50",
                "--json",
                "number,title,url,body,files,labels",
                *repo_arg,
            ],
            cwd=repo_path,
        )
        issues = _gh_json(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--limit",
                "50",
                "--json",
                "number,title,url,body,labels",
                *repo_arg,
            ],
            cwd=repo_path,
        )
    except Exception as exc:
        logger.warning("github awareness failed for %s: %s", name, exc)
        return RepoAwareness(repo=name, error=str(exc))

    items: list[OpenItem] = []
    for pr in prs or []:
        files = []
        for f in pr.get("files") or []:
            if isinstance(f, dict) and f.get("path"):
                files.append(str(f["path"]))
            elif isinstance(f, str):
                files.append(f)
        labels = [str(x.get("name") if isinstance(x, dict) else x) for x in (pr.get("labels") or [])]
        items.append(
            OpenItem(
                kind="pr",
                number=int(pr.get("number") or 0),
                title=str(pr.get("title") or ""),
                url=str(pr.get("url") or ""),
                body=str(pr.get("body") or "")[:2000],
                files=files,
                labels=labels,
            )
        )
    for iss in issues or []:
        labels = [str(x.get("name") if isinstance(x, dict) else x) for x in (iss.get("labels") or [])]
        items.append(
            OpenItem(
                kind="issue",
                number=int(iss.get("number") or 0),
                title=str(iss.get("title") or ""),
                url=str(iss.get("url") or ""),
                body=str(iss.get("body") or "")[:2000],
                labels=labels,
            )
        )
    return RepoAwareness(repo=name, items=items)


_TOKEN_RE = re.compile(r"[a-z0-9_]{4,}")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def overlap_reason(
    *,
    title: str,
    file_path: str = "",
    awareness: RepoAwareness,
    min_token_overlap: int = 3,
) -> str | None:
    """Return a human reason if this finding likely duplicates an open PR/issue."""
    file_name = Path(file_path).name.lower() if file_path else ""
    file_norm = file_path.replace("\\", "/").lower()
    title_tokens = _tokens(title)

    for it in awareness.items:
        # Same file already in an open PR
        for f in it.files:
            fn = f.replace("\\", "/").lower()
            if file_norm and (file_norm == fn or file_norm.endswith("/" + fn) or fn.endswith(file_norm)):
                return f"open {it.kind}#{it.number} already touches {Path(f).name}"
            if file_name and Path(f).name.lower() == file_name:
                return f"open {it.kind}#{it.number} already touches {file_name}"

        # Title / body token overlap
        hay = _tokens(it.haystack())
        overlap = title_tokens & hay
        # drop ultra-common noise
        overlap -= {"with", "from", "that", "this", "into", "when", "have", "should", "missing"}
        if len(overlap) >= min_token_overlap:
            return f"overlaps open {it.kind}#{it.number} ({it.title[:60]})"
    return None


def filter_findings_against_awareness(
    findings: list[Any],
    awareness: RepoAwareness,
) -> tuple[list[Any], list[tuple[Any, str]]]:
    """Split findings into keep vs skipped-with-reason."""
    keep: list[Any] = []
    skipped: list[tuple[Any, str]] = []
    for f in findings:
        title = str(getattr(f, "title", None) or (f.get("title") if isinstance(f, dict) else "") or "")
        file_path = str(
            getattr(f, "file_path", None)
            or (f.get("file") if isinstance(f, dict) else None)
            or (f.get("file_path") if isinstance(f, dict) else "")
            or ""
        )
        reason = overlap_reason(title=title, file_path=file_path, awareness=awareness)
        if reason:
            skipped.append((f, reason))
        else:
            keep.append(f)
    return keep, skipped
