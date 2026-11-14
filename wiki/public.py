"""Public export of the wiki: a gated, self-contained bundle for dragonhound.dev (#8, section 3).

``export()`` turns the internal wiki (``reports/wiki/``) into a bundle that a site build can
consume without reaching the auditor:

    <out>/manifest.json            repos, dev SHAs, pages, sha256 per file (the sync record)
    <out>/README.md                "generated, published at dragonhound.dev, do not edit"
    <out>/<repo>/<page>.md         the page as markdown (citations as [path:Lx-Ly])
    <out>/<repo>/<page>.html       an HTML fragment from the shared renderer, citations linked
                                   to the GitHub blob at the page's SHA

Scope is decided at export time from GitHub, never from a list: a repo is exported only if its
name starts with ``argus-``, GitHub reports it public, and it has a built wiki. A failed lookup
counts as private. "Notable findings" (built from audit data) is never exported, nor is the
internal INDEX (dropped claims, citation stats, cited-file hashes).

The opsec gate fails closed: a page is refused whole, never redacted, if it contains a private
or link-local IP, a home or mount path, an email address, a LAN hostname, the name of a private
ecosystem repo that isn't an argus repo, or any term from the private deny-list (a file outside
the repo; the export refuses to run without it). Refused pages are listed in a report written
*outside* the bundle, since the report quotes what leaked. A repo with any refused page is left
out entirely rather than shipped half-documented.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PUBLIC_PAGES = ("overview", "architecture", "interfaces", "integration")  # never "findings"
DEFAULT_DENYLIST = Path.home() / ".config" / "ai-code-auditor" / "opsec-denylist.txt"
OWNER = "smk762"

# an address may end a sentence ("… at 192.168.1.9."), so only a dot followed by a digit
# means "part of a longer dotted number"
_IPV4 = re.compile(r"(?<!\d)(?<!\d\.)(\d{1,3}(?:\.\d{1,3}){3})(?!\d)(?!\.\d)")
_IPV6_LOCAL = re.compile(r"(?i)\b(fe80|fc[0-9a-f]{2}|fd[0-9a-f]{2}):[0-9a-f:]+")
_PATHS = re.compile(r"(?<![\w.])(/home/[\w.-]+|/Users/[\w.-]+|/root/|/mnt/[\w.-]+|/media/[\w.-]+|[A-Za-z]:\\Users\\)")
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+\.)+[A-Za-z]{2,}\b")
_EMAIL_OK = re.compile(r"(?i)@(example\.(com|org|net)|[\w.-]+\.invalid|users\.noreply\.github\.com)$")
_LAN_HOST = re.compile(r"(?i)\b[a-z0-9-]+\.(lan|local|internal|home\.arpa|localdomain)\b")
_MERMAID = re.compile(r"^```mermaid\n.*?^```\n?", re.M | re.S)


@dataclass
class Refusal:
    repo: str
    page: str
    reason: str
    match: str


@dataclass
class ExportReport:
    exported: dict[str, list[str]] = field(default_factory=dict)
    refused: list[Refusal] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    out_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"exported": self.exported, "skipped": self.skipped, "out_dir": self.out_dir,
                "refused": [r.__dict__ for r in self.refused]}


# ── the gate ─────────────────────────────────────────────────────────────────

def load_denylist(path: str | Path | None = None) -> list[str]:
    """Private terms (machine names, personal names, …), one per line, # comments allowed."""
    p = Path(path or os.getenv("AI_AUDIT_OPSEC_DENYLIST") or DEFAULT_DENYLIST)
    if not p.is_file():
        raise FileNotFoundError(
            f"opsec deny-list not found at {p}: the public export won't run without it. "
            "Create it (one private term per line) or set AI_AUDIT_OPSEC_DENYLIST.")
    terms = [ln.split("#", 1)[0].strip() for ln in p.read_text(encoding="utf-8").splitlines()]
    return [t for t in terms if t]


def _bad_ip(text: str) -> str | None:
    for m in _IPV4.finditer(text):
        try:
            ip = ipaddress.IPv4Address(m.group(1))
        except ValueError:
            continue
        if (ip.is_private or ip.is_link_local) and not ip.is_loopback and not ip.is_unspecified:
            return m.group(1)
    m = _IPV6_LOCAL.search(text)
    return m.group(0) if m else None


def gate(text: str, denylist: list[str], private_names: list[str]) -> tuple[str, str] | None:
    """(reason, matched text) for the first thing that must not go public, else None.

    Names and terms match unless they are part of a longer alphanumeric word, so
    ``VOLUPTAS_URL`` and ``argus-voluptas`` name voluptas but ``dragonhound`` doesn't name Dragon.
    """
    if ip := _bad_ip(text):
        return "private or link-local IP", ip
    if m := _PATHS.search(text):
        return "home or mount path", m.group(0)
    for m in _EMAIL.finditer(text):
        if not _EMAIL_OK.search(m.group(0)):
            return "email address", m.group(0)
    if m := _LAN_HOST.search(text):
        return "LAN hostname", m.group(0)
    for name in private_names:
        if re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text):
            return "private repo named", name
    for term in denylist:
        if re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text):
            return "deny-listed term", term
    return None


# ── scrubbing the inputs of the public variant ("placeholders at the source") ──

@dataclass
class Scrubber:
    """Replaces private names and deny-listed terms in model inputs with neutral wording.

    Private ecosystem repos are described by their area in the ecosystem doc ("an external
    ComfyUI service"); deny-listed terms become "an internal host". The gate still checks the
    output: scrubbing lowers the refusal rate, it isn't the safety net.
    """
    denylist: list[str]
    private: dict[str, str]  # private repo name → neutral description

    def __call__(self, text: str) -> str:
        for name, desc in self.private.items():
            text = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", desc, text)
        for term in self.denylist:
            text = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", "an internal host", text)
        text = _PATHS.sub("<path>", text)
        text = _LAN_HOST.sub("<host>", text)
        for m in list(_IPV4.finditer(text)):
            if _bad_ip(m.group(1)):
                text = text.replace(m.group(1), "<host>")
        return text


def public_ecosystem_doc(text: str, private_names: list[str]) -> str:
    """The ecosystem doc for public pages: no deployment or ops sections, no private rows."""
    for marker in ("\n## Deployment notes", "\n## Night conductor"):
        if marker in text:
            text = text.split(marker, 1)[0]
    keep = []
    for line in text.splitlines():
        low = line.lower()
        if "not scanned (integration only)" in low:
            continue
        if line.lstrip().startswith("|") and any(re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", low) for n in private_names):
            continue  # a table row describing a private repo
        keep.append(line)
    return "\n".join(keep).rstrip() + "\n"


def scrubber_for(denylist: list[str], private_names: list[str], ecosystem_text: str) -> Scrubber:
    areas = {m.group(2): m.group(1) for m in re.finditer(
        r"^\|\s*([^|]+?)\s*\|\s*\[([\w.-]+)\]\([^)]+\)\s*\|", ecosystem_text, re.M)}
    return Scrubber(denylist, {n: (f"an external {areas[n]} service" if n in areas else "an external service")
                               for n in private_names})


def public_inputs(denylist_path: str | Path | None = None,
                  visibility: Callable[[str], str | None] | None = None) -> tuple[Scrubber, str, dict[str, str | None]]:
    """(scrubber, public ecosystem doc, visibility per enabled repo) for a public build."""
    from auditor.config import load_ecosystem_config
    from auditor.merge_poll import github_token
    from wiki.pages import ECOSYSTEM_DOC

    denylist = load_denylist(denylist_path)
    token = github_token()
    lookup = visibility or (lambda r: github_visibility(r, token))
    vis = {r.name: lookup(r.name) for r in load_ecosystem_config().repos if r.enabled}
    private_names = sorted(n for n, v in vis.items() if v != "public" and not n.startswith("argus-"))
    raw = ECOSYSTEM_DOC.read_text(encoding="utf-8") if ECOSYSTEM_DOC.is_file() else ""
    scrub = scrubber_for(denylist, private_names, raw)
    return scrub, scrub(public_ecosystem_doc(raw, private_names)), vis


# ── scope ────────────────────────────────────────────────────────────────────

def github_visibility(repo: str, token: str = "") -> str | None:
    """'public' / 'private' from the GitHub API; None if the lookup fails (treated as private)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"https://api.github.com/repos/{OWNER}/{repo}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-code-auditor",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("visibility")
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ── export ───────────────────────────────────────────────────────────────────

def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _pypi_name(repo_path: str | None) -> str | None:
    import tomllib

    p = Path(repo_path or "") / "pyproject.toml"
    try:
        return (tomllib.loads(p.read_text(encoding="utf-8")).get("project") or {}).get("name")
    except (OSError, ValueError):
        return None


def export(output_dir: str | Path, out: str | Path | None = None, *, denylist_path: str | Path | None = None,
           visibility: Callable[[str], str | None] | None = None) -> ExportReport:
    from auditor.config import load_ecosystem_config
    from auditor.ui import render_markdown
    from auditor.ui_wiki import link_citations

    src = Path(output_dir) / "wiki"
    dest = Path(out or Path(output_dir) / "wiki_public")
    denylist = load_denylist(denylist_path)  # fail closed before touching anything
    from auditor.merge_poll import github_token

    token = github_token()
    lookup = visibility or (lambda r: github_visibility(r, token))
    repos = {r.name: r for r in load_ecosystem_config().repos if r.enabled}
    vis = {name: lookup(name) for name in sorted(repos)}
    private_names = sorted(n for n, v in vis.items() if v != "public" and not n.startswith("argus-"))

    report = ExportReport(out_dir=str(dest))
    staged: dict[str, dict[str, Any]] = {}
    for name in sorted(repos):
        if not name.startswith("argus-"):
            report.skipped[name] = "not an argus repo (internal only)"
            continue
        if vis[name] != "public":
            report.skipped[name] = f"GitHub visibility: {vis[name] or 'lookup failed'}"
            continue
        # the public variant (built from scrubbed inputs) wins over the internal pages
        base = src / "_public" / name if (src / "_public" / name / "INDEX.json").is_file() else src / name
        try:
            idx = json.loads((base / "INDEX.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report.skipped[name] = "no wiki built"
            continue
        pages, refused = [], False
        for entry in idx.get("pages", []):
            if entry.get("slug") not in PUBLIC_PAGES or entry.get("stale"):
                continue
            md = (base / entry["file"]).read_text(encoding="utf-8")
            md = _MERMAID.sub("", md)  # diagrams are omitted until they are rendered to SVG at build time
            hit = gate(md, denylist, private_names)
            if hit:
                report.refused.append(Refusal(name, entry["slug"], *hit))
                refused = True
                continue
            sha = entry.get("source_sha") or idx.get("source_sha")
            html = render_markdown(link_citations(md, f"{OWNER}/{name}", sha, getattr(repos[name], "subpath", "") or ""))
            pages.append({"slug": entry["slug"], "title": entry["title"], "md": md, "html": html,
                          "source_sha": sha, "generated_at": entry.get("generated_at")})
        if refused:
            report.skipped[name] = "a page was refused by the opsec gate (repo left out whole)"
            continue
        if not pages:
            report.skipped[name] = "no exportable pages"
            continue
        staged[name] = {"sha": idx.get("source_sha"), "branch": idx.get("branch"), "built_at": idx.get("built_at"),
                        "github": f"https://github.com/{OWNER}/{name}", "pypi": _pypi_name(repos[name].path),
                        "pages": pages}

    # write the bundle from scratch so removed repos and pages disappear (--delete semantics)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    manifest: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "generator": "ai-code-auditor wiki export", "repos": {}}
    for name, r in staged.items():
        (dest / name).mkdir()
        files = []
        for p in r["pages"]:
            for ext, body in (("md", p["md"]), ("html", p["html"])):
                (dest / name / f"{p['slug']}.{ext}").write_text(body, encoding="utf-8")
            files.append({"slug": p["slug"], "title": p["title"], "md": f"{name}/{p['slug']}.md",
                          "html": f"{name}/{p['slug']}.html", "sha256_md": _sha256(p["md"]),
                          "sha256_html": _sha256(p["html"]), "source_sha": p["source_sha"],
                          "generated_at": p["generated_at"]})
        manifest["repos"][name] = {k: r[k] for k in ("sha", "branch", "built_at", "github", "pypi")} | {"pages": files}
        report.exported[name] = [p["slug"] for p in r["pages"]]
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (dest / "README.md").write_text(
        "# Argus suite docs (generated)\n\nGenerated by ai-code-auditor from each repo's `dev` branch and published "
        "at https://dragonhound.dev. Do not edit: every export replaces this content. `manifest.json` records the "
        "source SHA and the sha256 of every page.\n", encoding="utf-8")
    # final belt-and-braces pass over everything written (the bundle must never ship a gate failure)
    for f in dest.rglob("*"):
        if f.is_file() and (hit := gate(f.read_text(encoding="utf-8"), denylist, private_names)):
            shutil.rmtree(dest)
            raise RuntimeError(f"opsec gate failed on the written bundle ({f.name}: {hit[0]}); bundle removed")
    report_path = Path(output_dir) / "wiki_public_report.json"  # outside the bundle: it quotes what leaked
    report_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return report
