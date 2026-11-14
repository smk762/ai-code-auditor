"""The wiki viewer: repo list, per-repo page tree, pages rendered server-side.

Pages come from ``reports/wiki/<repo>/`` (written by ``wiki.build``). Citations
``[path:Lx-Ly]`` become links to the GitHub blob at the SHA the page was built from, so a
reader lands on the exact lines the claim was checked against even after ``dev`` moves on.
The "behind" badge compares that SHA with the ``dev`` tip from the merge-poll state.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auditor import ui

CITE = re.compile(r"\[([\w./@+-]+?\.\w+):L(\d+)(?:-L?(\d+))?\]")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _wiki_root(output_dir: str | Path) -> Path:
    return Path(output_dir) / "wiki"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _repo_configs() -> dict[str, Any]:
    from auditor.config import load_ecosystem_config

    try:
        return {r.name: r for r in load_ecosystem_config().repos}
    except Exception:  # noqa: BLE001 — the viewer still works without config details
        return {}


def _poll(output_dir: str | Path) -> dict[str, Any]:
    from auditor.merge_poll import load_state, merge_poll_state_path

    try:
        return load_state(merge_poll_state_path(output_dir)).get("repos") or {}
    except Exception:  # noqa: BLE001
        return {}


def _behind(repo_cfg: Any, built_sha: str | None, tip_sha: str | None) -> dict[str, Any]:
    """How far the built SHA trails the dev tip. ``commits`` is None when the mirror doesn't
    have the tip yet (mirrors sync nightly), but ``behind`` is still known from the SHAs."""
    if not built_sha or not tip_sha:
        return {"behind": None, "commits": None, "tip_sha": tip_sha}
    if built_sha == tip_sha:
        return {"behind": False, "commits": 0, "tip_sha": tip_sha}
    commits = None
    path = Path(getattr(repo_cfg, "path", "") or "")
    if path.is_dir():
        try:
            r = subprocess.run(["git", "-C", str(path), "rev-list", "--count", f"{built_sha}..{tip_sha}"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                commits = int(r.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"behind": True, "commits": commits, "tip_sha": tip_sha}


def _github(slug: str | None, sha: str | None, subpath: str, path: str, a: int, b: int) -> str | None:
    if not slug or not sha:
        return None
    full = "/".join(p for p in ((subpath or "").strip("/"), path) if p)
    anchor = f"#L{a}" if a == b else f"#L{a}-L{b}"
    return f"https://github.com/{slug}/blob/{sha}/{full}{anchor}"


def link_citations(md: str, slug: str | None, sha: str | None, subpath: str = "") -> str:
    """Turn [path:Lx-Ly] citations into markdown links to GitHub (outside code fences)."""
    out, in_fence = [], False
    for line in md.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            def repl(m: re.Match) -> str:
                a = int(m.group(2))
                b = int(m.group(3) or a)
                label = f"{m.group(1)}:L{a}" + (f"-L{b}" if b != a else "")
                url = _github(slug, sha, subpath, m.group(1), a, b)
                return f"[`{label}`]({url})" if url else f"`{label}`"
            line = CITE.sub(repl, line)
        out.append(line)
    return "\n".join(out)


# ── data ─────────────────────────────────────────────────────────────────────

def wiki_repos(output_dir: str | Path) -> dict[str, Any]:
    """Every built repo with freshness, plus enabled repos that have no wiki (and why)."""
    root = _wiki_root(output_dir)
    top = _load(root / "INDEX.json") or {"repos": {}}
    cfgs, poll = _repo_configs(), _poll(output_dir)
    repos = []
    for name in sorted(top.get("repos") or {}):
        idx = _load(root / name / "INDEX.json")
        if not idx:
            continue
        llm = [p for p in idx.get("pages", []) if p.get("generator") == "llm"]
        tip = (poll.get(name) or {}).get("sha")
        repos.append({
            "repo": name,
            "built_at": idx.get("built_at"),
            "source_sha": idx.get("source_sha"),
            "branch": idx.get("branch"),
            "pages": [{"slug": p["slug"], "title": p["title"], "stale": bool(p.get("stale"))} for p in idx.get("pages", [])],
            "citations": sum((p.get("citations") or {}).get("total", 0) for p in llm),
            "dropped_claims": sum(len(p.get("dropped_claims") or []) for p in llm),
            "github_url": f"https://github.com/{poll[name]['slug']}" if (poll.get(name) or {}).get("slug") else None,
            **_behind(cfgs.get(name), idx.get("source_sha"), tip),
        })
    built = {r["repo"] for r in repos}
    missing = [{"repo": n, "reason": _missing_reason(c)} for n, c in sorted(cfgs.items())
               if getattr(c, "enabled", True) and n not in built]
    return {"repos": repos, "missing": missing, "updated_at": top.get("updated_at")}


def _missing_reason(cfg: Any) -> str:
    """Why an enabled repo has no wiki: usually it has no code the generator reads."""
    try:
        from wiki.sources import source_files

        if not source_files(cfg):
            return "no Python, JS or TS source to document (the generator reads only those)"
    except Exception:  # noqa: BLE001 — e.g. mirror missing
        return "mirror not available"
    return "not built yet"


def repo_index(output_dir: str | Path, repo: str) -> dict[str, Any] | None:
    idx = _load(_wiki_root(output_dir) / repo / "INDEX.json")
    if idx is None:
        return None
    tip = (_poll(output_dir).get(repo) or {})
    return idx | {"github_slug": tip.get("slug"), **_behind(_repo_configs().get(repo), idx.get("source_sha"), tip.get("sha"))}


def page_data(output_dir: str | Path, repo: str, slug: str) -> dict[str, Any] | None:
    """Markdown and metadata for one page; None if repo or page isn't in the index."""
    idx = repo_index(output_dir, repo)
    if idx is None:
        return None
    entry = next((p for p in idx.get("pages", []) if p["slug"] == slug), None)
    if entry is None:
        return None
    path = _wiki_root(output_dir) / repo / entry["file"]
    if path.parent != _wiki_root(output_dir) / repo or not path.is_file():
        return None
    return {"repo": repo, "page": entry, "markdown": path.read_text(encoding="utf-8"), "index": idx}


# ── rendering ────────────────────────────────────────────────────────────────

def _ago(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    s = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return f"{int(s // n)} {unit} ago"
    return "just now"


def _behind_badge(b: dict[str, Any]) -> str:
    if b.get("behind") is None:
        return ""
    if not b["behind"]:
        return '<span class="badge ok-badge">up to date with dev</span>'
    n = b.get("commits")
    text = f"{n} commit{'s' if n != 1 else ''} behind dev" if n is not None else "behind dev"
    return f'<span class="badge warn-badge" title="dev tip {html.escape(str(b.get("tip_sha") or "")[:10])}">{html.escape(text)}</span>'


def render_wiki_home(output_dir: str | Path) -> str:
    data = wiki_repos(output_dir)
    cards = []
    for r in data["repos"]:
        first = r["pages"][0]["slug"] if r["pages"] else "overview"
        links = " · ".join(f'<a href="/ui/wiki/{html.escape(r["repo"])}/{html.escape(p["slug"])}">{html.escape(p["title"])}</a>'
                           for p in r["pages"])
        cards.append(
            f'<article class="card span-4 wiki-card"><h2><a href="/ui/wiki/{html.escape(r["repo"])}/{html.escape(first)}">'
            f'{html.escape(r["repo"])}</a></h2>'
            f'<p class="muted"><span class="mono">{html.escape(str(r["source_sha"] or "")[:8])}</span> · built {_ago(r["built_at"])} '
            f'{_behind_badge(r)}</p><p class="pages">{links}</p>'
            f'<p class="muted small">{r["citations"]:,} citations checked · {r["dropped_claims"]} claims dropped</p></article>')
    missing = "".join(f'<li><b>{html.escape(m["repo"])}</b>: {html.escape(m["reason"])}</li>' for m in data["missing"])
    body = ('<section class="grid">' + ("".join(cards) or '<div class="card span-12 empty">No wiki built yet. '
            'Run <code>ai-audit wiki build --dry-run</code>.</div>') + "</section>")
    if missing:
        body += f'<section class="card" style="margin-top:12px"><h2>Without a wiki</h2><ul class="missing">{missing}</ul></section>'
    sub = f"{len(data['repos'])} repos · generated from the code, every claim cited and checked · updated {_ago(data['updated_at'])}"
    return ui.render_page("wiki_home.html", "/ui/wiki", {"sub": html.escape(sub), "body": body})


def render_wiki_page(output_dir: str | Path, repo: str, slug: str) -> str | None:
    d = page_data(output_dir, repo, slug)
    if d is None:
        return None
    idx, entry = d["index"], d["page"]
    cfg = _repo_configs().get(repo)
    sha = entry.get("source_sha") or idx.get("source_sha")
    md = link_citations(d["markdown"], idx.get("github_slug"), sha, getattr(cfg, "subpath", "") or "")
    current, stale = ' aria-current="page"', ' <span class="badge">stale</span>'
    nav = "".join(
        f'<li><a href="/ui/wiki/{html.escape(repo)}/{html.escape(p["slug"])}"'
        f'{current if p["slug"] == slug else ""}>{html.escape(p["title"])}'
        f'{stale if p.get("stale") else ""}</a></li>'
        for p in idx.get("pages", []))
    gh = idx.get("github_slug")
    meta = [f'<span class="mono">{html.escape(repo)} @ {html.escape(str(sha or "")[:8])}</span>']
    if gh and sha:
        meta.append(f'<a href="https://github.com/{html.escape(gh)}/tree/{html.escape(sha)}">GitHub</a>')
    meta.append(f"built {_ago(entry.get('generated_at'))}")
    badge = _behind_badge(idx)
    c = entry.get("citations") or {}
    dropped = entry.get("dropped_claims") or []
    if entry.get("generator") == "data":
        foot = "Built from the audit data (findings, packs, violations), not by a model."
    else:
        foot = (f"Written by {html.escape(str(entry.get('model') or 'a model'))} from file notes, then checked against the "
                f"cited code. {c.get('total', 0)} citations, all pointing at real lines"
                + (f"; {c.get('named', 0)} name a symbol found in the cited lines" if c.get("named") else "") + ".")
        if dropped:
            items = "".join(f"<li><span class=\"badge\">{html.escape(x.get('reason', ''))}</span> {html.escape(x.get('claim', '')[:300])}</li>"
                            for x in dropped)
            foot += (f'<details><summary>{len(dropped)} claim{"s" if len(dropped) != 1 else ""} dropped by the citation check</summary>'
                     f'<ul class="dropped">{items}</ul></details>')
    return ui.render_page("wiki_page.html", "/ui/wiki", {
        "title": html.escape(f"{entry['title']} · {repo}"),
        "repo": html.escape(repo),
        "pages": nav,
        "meta": " · ".join(meta) + (" " + badge if badge else ""),
        "content": ui.render_markdown(md),
        "foot": foot,
    })
