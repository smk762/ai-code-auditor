"""Keep the wiki fresh: rebuild only stale pages, at most weekly per repo, then export + publish.

A page is stale when the text at a line range it cites changed, or the cited file
disappeared (``cited_ranges`` in the repo's INDEX; older entries fall back to whole-file
hashes in ``cited_files``). Those are rebuilt only when the mirror moved past the SHA the repo
was built from, and at most once every ``AI_AUDIT_WIKI_MIN_INTERVAL_DAYS`` days (default 7)
unless forced. Pages whose last build failed, or that don't exist yet, are retried on the next
run regardless: they aren't waiting on new code. That retry also rebuilds the repo's other
stale pages, because the build stamps the repo with the mirror's HEAD. Both variants are kept:
the internal wiki and, for public argus repos, the public one.

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
from wiki.citations import range_hash
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


def _text_at(repo, sha: str, path: str) -> str | None:
    """A file as it was at ``sha`` in the mirror (scan-root-relative path), or None."""
    sub = (getattr(repo, "subpath", "") or "").strip("/")
    try:
        r = subprocess.run(["git", "-C", repo.path, "show", f"{sha}:{'/'.join(p for p in (sub, path) if p)}"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _ranges_from_history(repo, entry: dict[str, Any], page_dir: Path | None) -> dict[str, dict[str, Any]] | None:
    """cited_ranges for an entry built before they were recorded: the page's citations, hashed
    against the cited files as they were at the entry's source SHA (read from the mirror)."""
    from wiki.citations import _ranges

    sha = entry.get("source_sha")
    if not sha or page_dir is None:
        return None
    md = read_text(page_dir / entry.get("file", ""))
    if md is None:
        return None
    out: dict[str, dict[str, Any]] = {}
    for path, ranges in _ranges(md).items():
        old = _text_at(repo, sha, path)
        h = range_hash(old.splitlines(), ranges) if old is not None else None
        if h is None:
            return None  # can't reconstruct: use the whole-file check instead
        out[path] = {"ranges": ranges, "hash": h}
    return out


def stale_pages(repo, idx: dict[str, Any] | None, page_dir: Path | None = None) -> tuple[set[str], dict[str, str]]:
    """(pages to rebuild, why per page) from the cited line ranges (or, failing that, the
    cited-file hashes) in the index."""
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
        elif ranges := (e.get("cited_ranges") or _ranges_from_history(repo, e, page_dir)):
            # precise: only the cited lines matter
            for path, info in ranges.items():
                text = read_text(root / path)
                if text is None:
                    why[page] = f"cited file removed: {path}"
                    break
                if range_hash(text.splitlines(), info["ranges"]) != info["hash"]:
                    why[page] = f"cited lines changed: {path}"
                    break
        else:
            for path, h in (e.get("cited_files") or {}).items():  # older entries: whole-file hashes
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
            have = {p["slug"]: p for p in (idx or {}).get("pages", [])}
            incomplete = any(p not in have or have[p].get("stale") for p in pages_mod.PAGES)
            # Pages that failed or don't exist yet aren't waiting on new code, so they skip the
            # mirror and interval gates. stale_pages() still runs over every page: the build
            # stamps the repo with the mirror's HEAD, so a page whose cited lines changed has to
            # be rebuilt in the same build or the stamp would hide it.
            if idx is not None and not force and not incomplete:
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
            pages, why = (set(pages_mod.PAGES), {p: "forced" for p in pages_mod.PAGES}) if force else stale_pages(
                repo, idx, _wiki(out) / ("_public" if variant == "public" else "") / repo.name)
            if not pages:
                result.skipped[key] = "mirror moved, but no cited file changed"
                continue
            result.items.append(PlanItem(repo.name, variant, sorted(pages), why))
    return result


def _live_repos(out: Path) -> set[str]:
    """Repos whose pages are in the published bundle (their rebuilds reach the public site)."""
    try:
        return set(json.loads((out / "wiki_public" / "manifest.json").read_text(encoding="utf-8")).get("repos") or {})
    except (OSError, ValueError):
        return set()


def select_within_budget(items: list[PlanItem], estimates: dict[tuple[str, str], float], budget: float,
                         live: set[str], built_at: dict[tuple[str, str], str]) -> tuple[list[PlanItem], list[PlanItem]]:
    """(items to build, items deferred) so the total estimate fits ``budget``.

    Pages that reach the public bundle go first: the public variant, and the internal pages of
    published repos that have no public variant. Then everything else. Within a tier, the
    oldest build first. An item too big for what's left is deferred, and smaller ones after it
    can still fit.
    """
    variants = {(i.repo, i.variant) for i in items}

    def tier(i: PlanItem) -> int:
        reaches_site = i.variant == "public" or (i.repo in live and (i.repo, "public") not in variants
                                                 and not built_at.get((i.repo, "public")))
        return 0 if reaches_site else 1

    chosen, deferred, spent = [], [], 0.0
    for i in sorted(items, key=lambda i: (tier(i), built_at.get((i.repo, i.variant)) or "", i.repo)):
        cost = estimates.get((i.repo, i.variant), 0.0)
        if spent + cost <= budget:
            chosen.append(i)
            spent += cost
        else:
            deferred.append(i)
    return chosen, deferred


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
                   "estimate_usd": 0.0, "deferred": []}
        # price each item, and if the lot won't fit the wiki lane, build the most valuable part
        # (a busy week then defers some pages instead of the budget check refusing everything)
        from auditor.spend_ledger import get_ledger

        estimates = {(i.repo, i.variant): build([i.repo], out, mode=mode, dry_run=True, public=i.variant == "public",
                                                pages={i.repo: set(i.pages)}).estimate_usd for i in pl.items}
        built_at = {(i.repo, i.variant): (_index(out, i.repo, i.variant == "public") or {}).get("built_at") or ""
                    for i in pl.items} | {(i.repo, "public"): (_index(out, i.repo, True) or {}).get("built_at") or ""
                                          for i in pl.items}
        budget = get_ledger().remaining("wiki") * 0.95  # estimates run a little high; keep a margin anyway
        selected, deferred = select_within_budget(pl.items, estimates, budget, _live_repos(out), built_at)
        record["deferred"] = [asdict(i) | {"estimate_usd": round(estimates[(i.repo, i.variant)], 4)} for i in deferred]
        record["budget_usd"] = round(budget, 2)
        for variant in ("internal", "public"):
            items = [i for i in selected if i.variant == variant]
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
