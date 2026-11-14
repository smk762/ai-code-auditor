"""Keep the wiki fresh: rebuild only stale pages, at most weekly per repo, then export + publish.

A page is stale when a file it cites changed or disappeared (``cited_files`` in the repo's
INDEX holds a content hash per cited file). Those are rebuilt only when the mirror moved past
the SHA the repo was built from, and at most once every ``AI_AUDIT_WIKI_MIN_INTERVAL_DAYS``
days (default 7) unless forced. Pages whose last build failed, or that don't exist yet, are
retried on the next run regardless: they aren't waiting on new code. Both
variants are kept: the internal wiki and, for public argus repos, the public one.

After building, the public bundle is re-exported and published to the private docs repo; the
publish step only commits when the bundle's content hash changed, and dragonhound-site's pin
bump stays the review gate. One run at a time (a lock file); the last run is recorded in
``reports/wiki/_refresh.json`` for the API and UI.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from wiki import pages as pages_mod
from wiki.sources import content_hash, read_text

logger = logging.getLogger(__name__)


def min_interval() -> timedelta:
    return timedelta(days=float(os.getenv("AI_AUDIT_WIKI_MIN_INTERVAL_DAYS", "7")))


def _wiki(out: Path) -> Path:
    return out / "wiki"


def _index(out: Path, repo: str, public: bool) -> dict[str, Any] | None:
    p = _wiki(out) / ("_public" if public else "") / repo / "INDEX.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _head(path: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def stale_pages(repo, idx: dict[str, Any] | None) -> tuple[set[str], dict[str, str]]:
    """(pages to rebuild, why per page) from the cited-file hashes in the index."""
    from auditor.repo_resolver import resolve_scan_root

    if idx is None:
        return set(pages_mod.PAGES), {p: "never built" for p in pages_mod.PAGES}
    root = resolve_scan_root(repo)
    entries = {p["slug"]: p for p in idx.get("pages", [])}
    why: dict[str, str] = {}
    for page in pages_mod.PAGES:
        e = entries.get(page)
        if e is None:
            why[page] = "no page yet"
        elif e.get("stale"):
            why[page] = "last build failed"
        else:
            for path, h in (e.get("cited_files") or {}).items():
                text = read_text(root / path)
                if text is None:
                    why[page] = f"cited file removed: {path}"
                    break
                if content_hash(text) != h:
                    why[page] = f"cited file changed: {path}"
                    break
    return set(why), why


@dataclass
class PlanItem:
    repo: str
    variant: str  # internal | public
    pages: list[str]
    reasons: dict[str, str]


@dataclass
class Plan:
    items: list[PlanItem] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # "repo/variant" → why


def _refused_by_last_export(out: Path) -> set[str]:
    try:
        rep = json.loads((out / "wiki_public_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {r["repo"] for r in rep.get("refused") or []}


def plan(output_dir: str | Path, repo_names: list[str] | None = None, *, force: bool = False,
         all_public: bool = False, now: datetime | None = None) -> Plan:
    """What a refresh would rebuild.

    The public variant (scrubbed inputs) costs as much as the internal one, so by default it is
    kept only where it's needed: repos that already have one, and repos whose internal pages the
    last export refused. Other public repos ship their internal pages, which pass the same gate
    on every export. ``all_public`` builds it for every public argus repo."""
    from auditor.config import load_ecosystem_config
    from wiki.sources import source_files

    out, now = Path(output_dir), now or datetime.now(timezone.utc)
    repos = [r for r in load_ecosystem_config().repos if r.enabled and (not repo_names or r.name in repo_names)]
    result = Plan()
    refused = _refused_by_last_export(out)
    for repo in repos:
        try:
            if not source_files(repo):
                result.skipped[repo.name] = "no Python/JS/TS source"
                continue
        except Exception as exc:  # noqa: BLE001 — a missing mirror skips just this repo
            result.skipped[repo.name] = f"cannot scan: {type(exc).__name__}"
            continue
        head = _head(repo.path)
        for variant in ("internal", "public") if repo.name.startswith("argus-") else ("internal",):
            key = f"{repo.name}/{variant}"
            idx = _index(out, repo.name, variant == "public")
            if variant == "public" and idx is None and not all_public and repo.name not in refused:
                result.skipped[key] = "internal pages pass the gate (no public variant needed)"
                continue
            if idx is not None and not force:
                # pages that failed or don't exist yet aren't waiting on new code: retry them now
                have = {p["slug"]: p for p in idx.get("pages", [])}
                incomplete = [p for p in pages_mod.PAGES if p not in have or have[p].get("stale")]
                if incomplete:
                    result.items.append(PlanItem(repo.name, variant, sorted(incomplete), {
                        p: "last build failed" if p in have else "no page yet" for p in incomplete}))
                    continue
                if head and idx.get("source_sha") == head:
                    result.skipped[key] = "up to date with the mirror"
                    continue
                try:
                    built = datetime.fromisoformat(idx.get("built_at") or "")
                except ValueError:
                    built = None
                if built and now - built < min_interval():
                    result.skipped[key] = f"built {(now - built).days}d ago (weekly at most)"
                    continue
            pages, why = (set(pages_mod.PAGES), {p: "forced" for p in pages_mod.PAGES}) if force else stale_pages(repo, idx)
            if not pages:
                result.skipped[key] = "mirror moved, but no cited file changed"
                continue
            result.items.append(PlanItem(repo.name, variant, sorted(pages), why))
    return result


@contextmanager
def _lock(out: Path) -> Iterator[None]:
    _wiki(out).mkdir(parents=True, exist_ok=True)
    f = open(_wiki(out) / ".refresh.lock", "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("a wiki refresh is already running") from exc
        yield
    finally:
        f.close()


def is_running(output_dir: str | Path) -> bool:
    p = _wiki(Path(output_dir)) / ".refresh.lock"
    if not p.exists():
        return False
    with open(p, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def last_run(output_dir: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads((_wiki(Path(output_dir)) / "_refresh.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def refresh(output_dir: str | Path, repo_names: list[str] | None = None, *, force: bool = False,
            mode: str = "batch", dry_run: bool = False, publish: bool = True, push: bool = True,
            all_public: bool = False, runner=None) -> dict[str, Any]:
    from wiki.build import build

    out = Path(output_dir)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record: dict[str, Any] = {"started_at": started, "repos": repo_names, "force": force, "dry_run": dry_run}
    with _lock(out):
        pl = plan(out, repo_names, force=force, all_public=all_public)
        record |= {"plan": [asdict(i) for i in pl.items], "skipped": pl.skipped, "builds": {}, "cost_usd": 0.0,
                   "estimate_usd": 0.0}
        for variant in ("internal", "public"):
            items = [i for i in pl.items if i.variant == variant]
            if not items:
                continue
            pages = {i.repo: set(i.pages) for i in items}
            rep = build(sorted(pages), out, mode=mode, dry_run=dry_run, runner=runner,
                        public=variant == "public", pages=pages)
            d = rep.to_dict()
            record["builds"][variant] = {"stopped": d["stopped"], "cost_usd": d["cost_usd"],
                                         "estimate_usd": d["estimate_usd"],
                                         "repos": {k: {f: v[f] for f in ("pages_written", "pages_failed", "skipped")}
                                                   for k, v in d["repos"].items()}}
            record["cost_usd"] += d["cost_usd"]
            record["estimate_usd"] += d["estimate_usd"]
        # rebuilt internal pages go to the audit RAG collection (AI_AUDIT_WIKI_RAG_INGEST=0 to skip)
        written = {repo: set(r.get("pages_written") or [])
                   for repo, r in (record["builds"].get("internal") or {}).get("repos", {}).items()}
        written = {repo: pages for repo, pages in written.items() if pages}
        if written and not dry_run:
            from wiki.rag import ingest as rag_ingest

            try:
                record["rag"] = rag_ingest(out, written)
            except Exception as exc:  # noqa: BLE001 — retrieval is a bonus; pages are built
                record["rag"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if publish and not dry_run:
            from wiki.public import export
            from wiki.publish import publish as do_publish

            try:
                ex = export(out)
                pub = do_publish(out / "wiki_public", push=push)
                record["publish"] = {"exported": len(ex.exported), "refused": len(ex.refused),
                                     "changed": pub["changed"], "pushed": pub["pushed"], "pin": pub["pin"]}
            except Exception as exc:  # noqa: BLE001 — pages are built; publishing can be retried
                logger.warning("wiki publish failed: %s", exc)
                record["publish"] = {"error": f"{type(exc).__name__}: {exc}"[:400]}
        record["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not dry_run:
            (_wiki(out) / "_refresh.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record
