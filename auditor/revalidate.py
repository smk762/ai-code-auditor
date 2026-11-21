"""Re-check merged fixes that were resolved on a waiver or left unverified (#58).

A fix is waived or left ``unverified`` when validate_patch couldn't show a test proving it,
often because the validator's environment couldn't import the project's tests at the time.
Once that is fixed (declared test deps, CI extras), the same merged fix can usually be proven.
Nothing else revisits it: a waived pack stays resolved, and the only later check is recurrence.

For each candidate pack this finds the agent PR that names it (the PR cache from
``trust_metrics``), takes the PR's change to the files its commits touched alongside the pack's
file, and sends it to ``POST /audit/validate_patch`` with ``base_ref`` = the commit the PR merged
onto (#91) and ``revalidate=true``. The API runs the evidence bar as usual. With ``revalidate``
it only records stronger evidence (a reproducing test, tests run, a test-only or non-code change)
and never demotes: a pack whose re-check finds nothing better is left exactly as it was.

A PR is matched by naming the pack, else (batch PRs often list fixes without ids) as an agent PR
in the pack's repo, merged after work on it started, that changes its file; ``matched`` says which.
The patch can carry other packs' fixes to the same files. The evidence then says "this PR's
change to these files makes a test go from failing to passing", which is what it is.

Git objects come from validate_base's fetch cache, never the mirror. ``run()`` writes
``revalidate.json`` with each pack's outcome.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from auditor.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

REPORT_NAME = "revalidate.json"
FINDING_KINDS_EXCLUDED = frozenset({"world_model", "architecture", "extraction", "invariants", "class_guard"})
_TEST_HINTS = ("test", "tests/", "__tests__", ".spec.", ".test.")


@dataclass
class Candidate:
    pack_id: str
    repo: str
    path: str  # relative to the repo root
    status: str
    evidence: str  # kind recorded on the pack ("waiver", "none", "")
    since: str = ""  # when work on it started (first status change): a fix merges after this


@dataclass
class Outcome:
    pack_id: str
    repo: str
    result: str  # upgraded | unchanged | no_pr | no_patch | error
    detail: str = ""
    pr: str = ""
    matched: str = ""  # "named": the PR names the pack; "file": an agent PR in its repo changed its file
    base: str = ""
    evidence: str = ""
    files: list[str] = field(default_factory=list)


def _rel(repo: str, path: str) -> str:
    from auditor.trust_metrics import rel_path

    return rel_path(repo, path)


def _map(value: Any) -> dict[str, Any]:
    """*value* if it is a mapping, else {}: a hand-edited or truncated pack may hold anything."""
    return value if isinstance(value, dict) else {}


def candidates(output_dir: str | Path, *, repo: str = "") -> list[Candidate]:
    """Finding packs resolved on a waiver, or unverified. A pack that can't be read or isn't a
    mapping (not UTF-8, bad YAML, a list or scalar at the top) is skipped, not fatal to the scan."""
    out = []
    for f in sorted(Path(output_dir, "agent_packs").glob("pack-*.yaml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if not isinstance(d, dict):
            logger.warning("revalidate: skipping %s: not a mapping", f.name)
            continue
        if d.get("kind") in FINDING_KINDS_EXCLUDED:
            continue
        name = str(_map(d.get("repo")).get("name") or "")
        if repo and name != repo:
            continue
        status = str(d.get("status") or "")
        kind = str(_map(_map(d.get("verification")).get("evidence")).get("kind") or "")
        if not ((status == "resolved" and kind == "waiver") or status == "unverified"):
            continue
        path = str(_map(d.get("location")).get("file_path") or "")
        if not path:
            continue
        history = d.get("status_history")
        hist = [str(h.get("at") or "") for h in (history if isinstance(history, list) else [])
                if isinstance(h, dict) and h.get("at")]
        out.append(Candidate(str(d.get("work_item_id")), name, _rel(name, path), status, kind, min(hist, default="")))
    return out


def prs_by_pack(prs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Merged PRs naming each pack, newest first."""
    by: dict[str, list[dict[str, Any]]] = {}
    for pr in sorted(prs, key=lambda p: p.get("merged_at") or "", reverse=True):
        if not pr.get("merge_sha"):
            continue
        for pid in pr.get("pack_ids") or []:
            by.setdefault(pid, []).append(pr)
    return by


def prs_for(c: Candidate, named: dict[str, list[dict[str, Any]]], prs: list[dict[str, Any]]) -> list[tuple[str, dict]]:
    """PRs that may hold *c*'s fix: those naming it, then (many batch PRs list fixes without pack
    ids) agent PRs in its repo merged after work on it started, oldest first. ``fix_patch`` drops
    any that don't change the pack's file."""
    out = [("named", pr) for pr in named.get(c.pack_id, [])]
    seen = {pr.get("url") for _, pr in out}
    since = _ts(c.since)
    for pr in sorted(prs, key=lambda p: p.get("merged_at") or ""):
        if not (pr.get("repo") == c.repo and pr.get("merge_sha") and pr.get("url") not in seen):
            continue
        if since is not None:
            merged = _ts(pr.get("merged_at"))
            if merged is None or merged < since:
                continue
        out.append(("file", pr))
    return out


def _ts(value: Any) -> datetime | None:
    """An ISO timestamp as an aware datetime, offsets honoured; naive means UTC. None if unparsable."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _git(git_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", f"--git-dir={git_dir}", *args], capture_output=True, text=True,
                          timeout=120, check=False, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})


def _has(git_dir: Path, sha: str) -> bool:
    return _git(git_dir, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _files(git_dir: Path, *args: str) -> list[str]:
    r = _git(git_dir, *args)
    return [x for x in r.stdout.splitlines() if x] if r.returncode == 0 else []


def _merge_base_of_pr(git_dir: Path, merge: str, pr: dict[str, Any]) -> tuple[str, list[str]]:
    """``(base, rebased_shas)``: the commit *pr* merged onto, and for a rebase merge the commits it
    landed as. ``("", [])`` when that can't be told from the cache.

    A merge commit's first parent is the base; so is a squash commit's. GitHub's "rebase and merge"
    instead replays the PR's N commits onto dev, and ``merge_sha`` is the last of them, so the base
    is N first-parent steps back. A one-parent merge of a multi-commit PR is taken as a squash when
    its subject ends with GitHub's ``(#<number>)``, else as a rebase merge.
    """
    line = _git(git_dir, "rev-list", "--parents", "-n", "1", merge).stdout.split()
    parents = line[1:]
    if not parents:
        return "", []
    n = len(pr.get("commit_shas") or [])
    number = pr.get("number")
    if len(parents) > 1 or n <= 1 or not number:
        return parents[0], []
    subject = _git(git_dir, "log", "-1", "--format=%s", merge).stdout.strip()
    if subject.endswith(f"(#{number})"):
        return parents[0], []
    chain = [ln.split() for ln in
             _git(git_dir, "rev-list", "--first-parent", "--parents", "-n", str(n), merge).stdout.splitlines()]
    if any(len(c) > 2 for c in chain):
        return parents[0], []  # a merge commit among them: not a rebase merge of these N commits
    if len(chain) < n or len(chain[-1]) < 2:
        return "", []  # the cache's history is too short to reach the PR's base
    return chain[-1][1], [c[0] for c in chain]


def fix_patch(git_dir: Path, pr: dict[str, Any], path: str) -> tuple[str, str, list[str]]:
    """``(base_sha, patch, files)`` for *pr*'s fix to *path*: the PR's change, from the commit it
    merged onto, to the files of every PR commit that touched *path* (else *path* and the PR's
    test files). ``("", "", [])`` when the PR's merge isn't in the cache or doesn't touch *path*."""
    merge = str(pr.get("merge_sha") or "")
    if not merge or not _has(git_dir, merge):
        return "", "", []
    base, rebased = _merge_base_of_pr(git_dir, merge, pr)
    if not base:
        return "", "", []
    changed = set(_files(git_dir, "diff", "--name-only", base, merge))
    if path not in changed:
        return "", "", []
    files: set[str] = set()
    # A rebase merge's original commits are rarely in the cache; the commits it landed as are.
    for sha in rebased or pr.get("commit_shas") or []:
        if not _has(git_dir, sha):
            continue
        touched = _files(git_dir, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
        if path in touched:
            files.update(touched)
    if not files:
        files = {path} | {f for f in changed if any(h in f.lower() for h in _TEST_HINTS)}
    files &= changed  # the PR's net change: a file a later commit reverted has none
    patch = _git(git_dir, "diff", "--binary", "--full-index", base, merge, "--", *sorted(files)).stdout
    return base, patch, sorted(files)


Poster = Callable[[dict[str, Any]], dict[str, Any]]


def api_poster(api_base: str = "", timeout_s: int = 900, busy_wait_s: float | None = None,
               sleep: Callable[[float], None] = time.sleep) -> Poster:
    """POST to validate_patch. When every validation slot is taken (429), wait as long as the API's
    ``Retry-After`` says and try again, for up to *busy_wait_s* in all (``AI_AUDIT_REVALIDATE_BUSY_WAIT_S``,
    default 1800): agents' own validations come first, and a refusal says nothing about the fix."""
    from conductor.publish import public_base_url

    budget = busy_wait_s if busy_wait_s is not None else float(os.getenv("AI_AUDIT_REVALIDATE_BUSY_WAIT_S", "1800"))

    base = (api_base or public_base_url()).rstrip("/")
    headers = {"Content-Type": "application/json"}
    if tok := os.getenv("AI_AUDIT_API_TOKEN", "").strip():
        headers["X-API-Token"] = tok

    def post(body: dict[str, Any]) -> dict[str, Any]:
        waited = 0.0
        while True:
            req = urllib.request.Request(f"{base}/audit/validate_patch", data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or waited >= budget:
                    raise
                try:
                    pause = float(exc.headers.get("Retry-After") or 30)
                except ValueError:
                    pause = 30.0
                pause = max(1.0, min(pause, budget - waited))
                sleep(pause)
                waited += pause

    return post


def revalidate_one(c: Candidate, prs: list[tuple[str, dict[str, Any]]], git_dir: Path | None, post: Poster, *,
                   timeout_s: int = 300, dry_run: bool = False) -> Outcome:
    if not prs:
        return Outcome(c.pack_id, c.repo, "no_pr", "no merged agent PR names this pack or followed it in its repo")
    if git_dir is None:
        return Outcome(c.pack_id, c.repo, "error", "no git cache for the repo")
    for how, pr in prs:
        base, patch, files = fix_patch(git_dir, pr, c.path)
        if not patch:
            continue
        o = Outcome(c.pack_id, c.repo, "unchanged", pr=str(pr.get("url") or ""), matched=how, base=base, files=files)
        if dry_run:
            o.detail = f"would send {len(files)} file(s)"
            return o
        try:
            body = post({"repo": c.repo, "patch": patch, "pack_id": c.pack_id, "base_ref": base,
                         "revalidate": True, "timeout_s": timeout_s})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            detail = exc.read().decode(errors="replace")[:300] if isinstance(exc, urllib.error.HTTPError) else str(exc)
            o.result, o.detail = "error", detail
            return o
        pk = body.get("pack") or {}
        o.evidence = str(pk.get("evidence") or "")
        if not body.get("passed"):
            o.detail = "validation did not pass: " + "; ".join((body.get("failure_summary") or [])[:3])
        elif pk.get("updated"):
            o.result, o.detail = "upgraded", str(pk.get("reason") or "")
        else:
            o.detail = str(pk.get("reason") or "")
        o.detail = f"tool={body.get('tool')}: {o.detail}"[:500]
        return o
    return Outcome(c.pack_id, c.repo, "no_patch", f"none of {len(prs)} PR(s) changed {c.path}")


def run(output_dir: str | Path, *, repo: str = "", limit: int = 0, dry_run: bool = False,
        refresh_prs: bool = False, post: Poster | None = None, timeout_s: int = 300,
        mirrors_path: str | Path = "config/repo_mirrors.yaml",
        git_dirs: dict[str, Path | None] | None = None, retry_days: float = 0) -> dict[str, Any]:
    """Re-check every candidate (or *limit*). With *retry_days*, skip packs whose PR was sent less
    than that many days ago and nothing came of it: re-running the same tests on the same code
    only changes once the repo's test env does."""
    from auditor import trust_metrics as tm

    out = Path(output_dir)
    attempts: dict[str, str] = {}
    try:
        attempts = dict(json.loads((out / REPORT_NAME).read_text()).get("attempts") or {})
    except (OSError, ValueError):
        pass
    prs = tm.load_prs_cache(out) or []
    if refresh_prs:
        try:
            prs = tm.refresh_prs(out, mirrors_path=mirrors_path)
        except Exception as exc:  # noqa: BLE001 — fall back to the cache, say why
            logger.warning("agent PR refresh failed, using cache: %s", exc)
    by_pack = prs_by_pack(prs)
    cands = candidates(out, repo=repo)
    skipped = 0
    if retry_days:
        cutoff = (datetime.now(UTC) - timedelta(days=retry_days)).isoformat()
        keep = [c for c in cands if attempts.get(c.pack_id, "") < cutoff]
        skipped, cands = len(cands) - len(keep), keep
    if limit:
        cands = cands[:limit]
    post = post or api_poster(timeout_s=timeout_s + 300)
    dirs = git_dirs if git_dirs is not None else {}
    if git_dirs is None:
        paths = tm._mirror_paths(mirrors_path)
        from auditor.validate_base import resolve_base

        for r in sorted({c.repo for c in cands}):
            p = paths.get(r)
            try:
                dirs[r] = resolve_base(p, output_dir=out).git_dir if p else None
            except Exception as exc:  # noqa: BLE001 — that repo's packs report an error
                logger.warning("revalidate: no git cache for %s: %s", r, exc)
                dirs[r] = None
    outcomes = [revalidate_one(c, prs_for(c, by_pack, prs), dirs.get(c.repo), post,
                               timeout_s=timeout_s, dry_run=dry_run) for c in cands]
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.result] = counts.get(o.result, 0) + 1
    now = datetime.now(UTC).isoformat()
    for o in outcomes:
        if o.result == "unchanged":  # sent and judged: don't resend before retry_days (an error retries next run)
            attempts[o.pack_id] = now
        else:
            attempts.pop(o.pack_id, None)
    report = {"at": now, "dry_run": dry_run, "repo": repo, "candidates": len(cands), "skipped_recent": skipped,
              "counts": counts, "outcomes": [asdict(o) for o in outcomes], "attempts": attempts}
    if not dry_run:
        atomic_write_text(out / REPORT_NAME, json.dumps(report, indent=1))
    return report


def enabled() -> bool:
    return os.getenv("AI_AUDIT_REVALIDATE", "1").strip().lower() not in ("0", "false", "no", "off")


def run_quietly(output_dir: str | Path | None = None) -> dict[str, Any]:
    """Pipeline tail: a bounded nightly pass (``AI_AUDIT_REVALIDATE_LIMIT`` packs, default 20; a
    pack is re-sent at most every ``AI_AUDIT_REVALIDATE_RETRY_DAYS``, default 7). Never raises."""
    if not enabled():
        return {"skipped": "AI_AUDIT_REVALIDATE=0"}
    try:
        r = run(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports", refresh_prs=True,
                limit=int(os.getenv("AI_AUDIT_REVALIDATE_LIMIT", "20")),
                retry_days=float(os.getenv("AI_AUDIT_REVALIDATE_RETRY_DAYS", "7")))
        return {k: r[k] for k in ("candidates", "skipped_recent", "counts")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("revalidate failed: %s", exc)
        return {"error": str(exc)[:300]}
