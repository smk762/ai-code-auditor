"""Ingest the internal wiki into the audit RAG collection, one chunk per ``##`` section.

Agents and the chat retrieve from rag-ingest's collection; whole pages are too broad to rank
well, so each section becomes a document with a stable id (``wiki::<repo>::<page>::<section>``),
a title naming repo, page and section, a link to that section in the wiki viewer, and tags
``kind:wiki``, ``repo:<name>``, ``page:<slug>``. Citations stay as ``[path:Lx-Ly]``.

Only the four generated content pages go in, from the internal variant; "Notable findings" is
built from audit data that changes nightly and packs already cover it. On by default; set
``AI_AUDIT_WIKI_RAG_INGEST=0`` to turn it off.

Re-sent sections replace their old chunks (rag-ingest keys them by id). Sections that were sent
before but no longer exist (a renamed heading, a dropped page) are retired with rag-ingest's
``POST /delete``, using the per-repo record in ``reports/rag_ingested.json``; when only some
pages are re-sent, only those pages' old sections are considered.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from wiki.pages import PAGES

_H2 = re.compile(r"^## +(.+?)\s*$", re.M)


def wiki_rag_enabled() -> bool:
    return os.getenv("AI_AUDIT_WIKI_RAG_INGEST", "1").strip().lower() not in {"0", "false", "no", "off"}


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", s) or "section"


def sections(md: str) -> list[tuple[str, str]]:
    """(heading, markdown) per ``##`` section; text before the first one is the intro."""
    marks = list(_H2.finditer(md))
    out: list[tuple[str, str]] = []
    intro = md[: marks[0].start()] if marks else md
    if intro.strip():
        out.append(("Introduction", intro.strip()))
    for i, m in enumerate(marks):
        body = md[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(md)].strip()
        out.append((m.group(1).strip(), body))
    return out


def _page_of(doc_id: str) -> str:
    parts = doc_id.split("::")
    return parts[2] if len(parts) > 3 else ""


def _index(output_dir: str | Path, repo: str) -> dict[str, Any] | None:
    """The repo's wiki INDEX.json, or None if it is missing, unreadable or not an object."""
    try:
        idx = json.loads((Path(output_dir) / "wiki" / repo / "INDEX.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return idx if isinstance(idx, dict) else None


def documents_for_repo(output_dir: str | Path, repo: str, pages: set[str] | None = None,
                       base_url: str = "") -> list[dict[str, Any]]:
    d = Path(output_dir) / "wiki" / repo
    idx = _index(output_dir, repo)
    if idx is None:
        return []
    docs: list[dict[str, Any]] = []
    entries = idx.get("pages")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):  # one bad entry must not cost the repo its other pages
            continue
        slug = entry.get("slug")
        if slug not in PAGES or (pages is not None and slug not in pages):
            continue
        if not isinstance(entry.get("file"), str):
            continue
        path = d / entry["file"]
        if path.parent != d or not path.is_file():
            continue
        try:
            md = path.read_text(encoding="utf-8")
        except (OSError, ValueError):  # unreadable or not UTF-8
            continue
        used: set[str] = set()
        for heading, body in sections(md):
            anchor = section_id = _slug(heading)
            n = 0
            while section_id in used:  # "Foo", "Foo" and "Foo 1" must not share an id
                n += 1
                section_id = f"{anchor}-{n}"
            used.add(section_id)
            docs.append({
                "id": f"wiki::{repo}::{slug}::{section_id}",
                "title": f"{repo} — {entry.get('title', slug)} — {heading}",
                "url": f"{base_url.rstrip('/')}/ui/wiki/{repo}/{slug}#{section_id}",
                "text": f"# {repo}: {entry.get('title', slug)}\n\n{body}",
            })
    return docs


def ingest(output_dir: str | Path, repos: dict[str, set[str] | None] | None = None) -> dict[str, Any]:
    """Post wiki sections per repo. ``repos`` maps repo → pages (None = all); default every built repo."""
    from auditor.rag_client import sync_documents
    from conductor.publish import public_base_url

    if not wiki_rag_enabled():
        return {"skipped": "AI_AUDIT_WIKI_RAG_INGEST=0"}
    root = Path(output_dir) / "wiki"
    if repos is None:
        repos = {p.parent.name: None for p in root.glob("*/INDEX.json") if not p.parent.name.startswith("_")}
    base = public_base_url()
    result: dict[str, Any] = {"repos": {}, "count": 0, "ingested": True}
    for repo, pages in sorted(repos.items()):
        docs = documents_for_repo(output_dir, repo, pages, base)
        considered = None if pages is None else (lambda i, p=frozenset(pages): _page_of(i) in p)
        if not docs:
            # Requested pages that are gone (deleted or renamed) still have to leave the index.
            # A whole-repo send that yields nothing is skipped instead: that is more likely a
            # failed build than a wiki that really is empty, and must not wipe the repo.
            if pages is not None and _index(output_dir, repo) is not None:
                from auditor.rag_client import recorded_ids, retire

                scope = f"wiki:{repo}"
                d = retire(output_dir, scope, {i for i in recorded_ids(output_dir, scope) if considered(i)})
                ok = bool(d.get("deleted"))
                result["repos"][repo] = {"ingested": ok, "count": 0, "retired": d.get("count", 0),
                                         **({} if ok else {"retire_error": d.get("error")})}
                result["retired"] = result.get("retired", 0) + d.get("count", 0)
                if not ok:  # the stale sections are still in the index: a retry is needed
                    result["ingested"] = False
                    if not os.getenv("INGEST_SHARED_SECRET", ""):  # as a skipped send: no point going on
                        result["error"] = d.get("error")
                        break
            continue
        r = sync_documents(output_dir, f"wiki:{repo}", docs, tags=["kind:wiki", f"repo:{repo}"],
                           considered=considered)
        result["repos"][repo] = {k: r.get(k) for k in ("ingested", "count", "retired", "status_code", "error",
                                                        "retire_error") if k in r}
        result["count"] += r.get("count", 0)
        result["retired"] = result.get("retired", 0) + r.get("retired", 0)
        if not r.get("ingested"):
            result["ingested"] = False
            if r.get("skipped"):  # e.g. no secret: no point trying the other repos
                result["error"] = r.get("error")
                break
    return result
