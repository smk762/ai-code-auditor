"""Build the hosted demo (#199) into demo/out/, ready for Cloudflare Pages: python3 demo/build.py [--out demo/out]

A static replay of demo/tape/tape.json (scripts/record_demo_tape.py records it): a run page and one page per
pack, with the shared design layer (vendor/dh, the strip pasted in). No inline script or style, so _headers'
CSP allows only this origin. Before writing, every string on the tape goes through the opsec gate again, with
the deny-list when ``AI_AUDIT_OPSEC_DENYLIST`` names one (CI passes it as a secret); a hit stops the build.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from auditor.demo_tape import OUTCOME_LABELS, OUTCOMES, check_public

TAPE = HERE / "tape" / "tape.json"
DH_FILES = ("dh-tokens.css", "dh-strip.css", "dh-badge.css", "dh-theme.js")
SITE = "Auditor"
REPO_URL = "https://github.com/smk762/ai-code-auditor"
DOCS_URL = "https://docs.dragonhound.dev/packages/auditor/"
POST_URL = "https://dragonhound.dev/updates/2026-11-28-auditor-one-night/"
SHORT = {"repro": "Proven", "test_only": "Test-only", "waiver": "Waiver", "wontfix": "Wontfix"}


def e(text: object) -> str:
    return escape(str(text if text is not None else ""), quote=True)


def md(text: object) -> str:
    """Escaped text with its `code` spans as <code>, the only markup the packs' prose uses."""
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", e(text))


def when(iso: str, with_date: bool = True) -> str:
    """'24 Nov 2026, 04:35 UTC' (or '04:35 UTC') from an ISO timestamp."""
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%-d %b %Y, %H:%M UTC" if with_date else "%H:%M UTC")


def strip_html() -> str:
    text = (HERE / "vendor" / "dh" / "dh-strip.html").read_text(encoding="utf-8")
    return text[text.index('<nav class="dh-strip"'):].strip()


def chip(outcome: str) -> str:
    return f'<span class="chip chip-{outcome}">{e(SHORT[outcome])}</span>'


def page(title: str, description: str, body: str, *, depth: int = 0) -> str:
    up = "../" * depth
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<meta name="build" content="{e(os.getenv('BUILD_SHA', 'local'))}">
<script src="{up}vendor/dh/dh-theme.js"></script>
<link rel="stylesheet" href="{up}vendor/dh/dh-tokens.css">
<link rel="stylesheet" href="{up}vendor/dh/dh-strip.css">
<link rel="stylesheet" href="{up}vendor/dh/dh-badge.css">
<link rel="stylesheet" href="{up}demo.css">
<link rel="icon" href="{up}vendor/dh/dh-mark.svg" type="image/svg+xml">
</head>
<body>
<a class="skip" href="#main">Skip to the content</a>
{strip_html()}
<header class="top">
  <p class="site"><a href="{up or './'}">{SITE}</a> <span class="dh-badge">recorded output</span></p>
  <div class="tools">
    <nav class="site-nav" aria-label="Site"><a href="{up}#packs">Packs</a><a href="{up}#how">How it works</a><a href="{POST_URL}">Write-up</a><a href="{DOCS_URL}">Docs</a></nav>
    <button type="button" class="theme" data-dh-theme-toggle aria-label="Dark theme" aria-pressed="false">
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
    </button>
  </div>
</header>
<main id="main">
{body}
</main>
<footer class="foot">
  <p class="links"><a href="{REPO_URL}">ai-code-auditor on GitHub</a><a href="{DOCS_URL}">Docs</a><a href="{POST_URL}">Write-up</a><a href="https://dragonhound.dev/">Dragonhound Labs</a></p>
  <p class="fine">A replay of one recorded run. Nothing here calls the auditor, a model or a repo: the findings, packs,
  diffs and test results are the ones the run and its pull request produced, shown in full, including the five
  findings that were wrong.</p>
</footer>
<script src="{up}demo.js" defer></script>
</body>
</html>
"""


# ── the run page ──────────────────────────────────────────────────────────────

def tiles(packs: list[dict]) -> str:
    cells = "".join(
        f'<li><a class="tile tile-{p["outcome"]}" href="packs/{e(p["id"])}/" title="{e(SHORT[p["outcome"]])}: {e(p["title"])}">'
        f'<span class="vh">{e(SHORT[p["outcome"]])}: {e(p["title"])}</span></a></li>' for p in packs)
    return f'<ol class="tiles" aria-label="Every finding, coloured by how it ended">{cells}</ol>'


def legend(counts: dict) -> str:
    rows = "".join(f'<li><span class="key key-{o}" aria-hidden="true"></span><strong>{counts[o]}</strong> {e(OUTCOME_LABELS[o])}</li>'
                   for o in OUTCOMES)
    return f'<ul class="legend">{rows}</ul>'


def timeline(tape: dict) -> str:
    packs, run = tape["packs"], tape["run"]
    claims = [h["at"] for p in packs for h in p["history"] if h["status"] == "claimed"]
    closes = [h["at"] for p in packs for h in p["history"] if h["status"] in ("resolved", "wontfix")]
    steps = [
        (run["started_at"], f"Nightly scan: {len(packs)} work packs",
         f"{run['repo']} at {run['scanned_sha'][:7]}, one of 31 repos that night. One pack per finding: the code, what to change, how it will be judged"),
        (min(claims), "An agent claims them", "Each claim is exclusive: a second agent would get a 409"),
        (max(closes), "Last pack validated", f"{tape['counts']['resolved']} resolved, {tape['counts']['wontfix']} marked wontfix with a note"),
        (run["pr"]["merged_at"], f"PR #{run['pr']['number']} merged", "Reviewed and merged by a person, one commit per fix"),
    ]
    items = "".join(f'<li><time datetime="{e(at)}">{e(when(at))}</time><strong>{e(what)}</strong><span>{e(sub)}</span></li>'
                    for at, what, sub in steps)
    return f'<ol class="timeline">{items}</ol>'


HOW_SVG = """<figure class="diagram">
<div class="diagram-scroll">
<svg viewBox="0 0 760 236" role="img" aria-labelledby="howt howd">
<title id="howt">How a work pack closes</title>
<desc id="howd">The nightly scan writes a work pack per finding. An agent claims it, writes a fix, and sends the patch to validate_patch, which runs the tests on a clean copy of dev and on dev plus the patch. A test that fails on dev and passes patched resolves the pack. Any other passing result leaves it unverified, which is still open work, unless a recorded waiver resolves it. A finding that is wrong is closed as wontfix with a note.</desc>
<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="dg-head" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="dg-box" x="8" y="40" width="104" height="48" rx="8"/><text class="dg-text" x="60" y="60" text-anchor="middle">Nightly</text><text class="dg-text" x="60" y="76" text-anchor="middle">scan</text>
<path class="dg-line" d="M112,64 H140" marker-end="url(#arr)"/>
<rect class="dg-box" x="142" y="40" width="104" height="48" rx="8"/><text class="dg-text" x="194" y="60" text-anchor="middle">Work</text><text class="dg-text" x="194" y="76" text-anchor="middle">pack</text>
<path class="dg-line" d="M246,64 H274" marker-end="url(#arr)"/>
<rect class="dg-box" x="276" y="40" width="104" height="48" rx="8"/><text class="dg-text" x="328" y="60" text-anchor="middle">Agent claims,</text><text class="dg-text" x="328" y="76" text-anchor="middle">writes a fix</text>
<path class="dg-line" d="M380,64 H408" marker-end="url(#arr)"/>
<rect class="dg-accent" x="410" y="8" width="170" height="112" rx="10"/>
<text class="dg-title" x="495" y="30" text-anchor="middle">validate_patch</text>
<rect class="dg-box" x="424" y="44" width="142" height="28" rx="6"/><text class="dg-text" x="495" y="62" text-anchor="middle">tests on dev</text>
<rect class="dg-box" x="424" y="80" width="142" height="28" rx="6"/><text class="dg-text" x="495" y="98" text-anchor="middle">tests on dev + patch</text>
<path class="dg-line" d="M580,44 C610,44 612,30 640,30" marker-end="url(#arr)"/>
<path class="dg-line" d="M580,96 C610,96 612,108 640,108" marker-end="url(#arr)"/>
<rect class="dg-ok" x="642" y="12" width="110" height="38" rx="8"/><text class="dg-text" x="697" y="28" text-anchor="middle">resolved</text><text class="dg-muted" x="697" y="43" text-anchor="middle">fails, then passes</text>
<rect class="dg-wait" x="642" y="90" width="110" height="38" rx="8"/><text class="dg-text" x="697" y="106" text-anchor="middle">unverified</text><text class="dg-muted" x="697" y="121" text-anchor="middle">still open work</text>
<path class="dg-line dg-dash" d="M697,128 V160" marker-end="url(#arr)"/>
<rect class="dg-box" x="618" y="162" width="134" height="38" rx="8"/><text class="dg-text" x="685" y="178" text-anchor="middle">resolved on a</text><text class="dg-text" x="685" y="193" text-anchor="middle">recorded waiver</text>
<path class="dg-line dg-dash" d="M194,88 V178 H260" marker-end="url(#arr)"/>
<rect class="dg-box" x="262" y="160" width="160" height="38" rx="8"/><text class="dg-text" x="342" y="176" text-anchor="middle">wrong finding:</text><text class="dg-text" x="342" y="191" text-anchor="middle">wontfix, with a note</text>
<text class="dg-muted" x="8" y="228">Dashed: the paths that need a person-readable reason on the record.</text>
</svg>
</div>
</figure>"""


def pack_rows(packs: list[dict]) -> str:
    groups = []
    for o in OUTCOMES:
        rows = "".join(
            f'<li class="row" data-outcome="{o}"><a href="packs/{e(p["id"])}/"><span class="row-title">{md(p["title"])}</span>'
            f'<span class="row-meta"><code>{e(p["file"])}</code> · {e(p["finding_type"])}</span></a></li>'
            for p in packs if p["outcome"] == o)
        if rows:
            groups.append(f'<section class="group" data-outcome="{o}"><h3>{chip(o)} {e(OUTCOME_LABELS[o])}</h3><ul class="rows">{rows}</ul></section>')
    filters = "".join(f'<button type="button" data-filter="{o}" aria-pressed="false">{e(SHORT[o])}</button>' for o in OUTCOMES)
    return (f'<div class="filters" role="group" aria-label="Show only" hidden><button type="button" data-filter="" aria-pressed="true">All</button>{filters}</div>'
            + "".join(groups))


def index_page(tape: dict) -> str:
    c, run, packs = tape["counts"], tape["run"], tape["packs"]
    body = f"""
<section class="hero">
  <h1>One night of findings, and the evidence behind every fix</h1>
  <p class="lede">ai-code-auditor scans every repo in the Argus suite each night and writes each finding up as a work
  pack for a coding agent. This is one real run, replayed: argus-scout on {e(when(run['started_at']).split(',')[0])},
  {c['findings']} findings, and how each one ended. A bug fix only counts when a test fails before it and passes after.</p>
  <p class="lede-links"><a href="{POST_URL}">Read the write-up</a> · <a href="{DOCS_URL}">Run it on your own repos</a></p>
  {tiles(packs)}
  {legend(c)}
</section>

<section class="band">
  <h2>The run</h2>
  {timeline(tape)}
</section>

<section class="band" id="how">
  <h2>How a pack closes</h2>
  {HOW_SVG}
  <div class="cards">
    <article class="card"><h3>{chip('repro')} Proven</h3><p>The patch has to bring a test that fails on a clean copy of
    <code>dev</code> and passes with the fix. Both trees are throwaway copies, never the live checkout, and only
    failures the patch introduces count against it. {c['repro']} of {c['resolved']} fixes here cleared that bar.</p></article>
    <article class="card"><h3>{chip('waiver')} Waivers and test-only</h3><p>{c['waiver'] + c['test_only']} findings were about
    weak tests. A stronger test passes on <code>dev</code>'s code too, so it can't show a failure there by design. Those
    resolve on a written waiver ({c['waiver']}) or as test-only ({c['test_only']}). Both are on the record, and a waiver
    never counts toward an agent earning more autonomy.</p></article>
    <article class="card"><h3>{chip('wontfix')} Wrong findings</h3><p>The scan is a language model reading code, and
    it is sometimes wrong: {c['wontfix']} of {c['findings']} findings here were. Each is closed as wontfix with the
    reason, and that keeps it closed when a later scan finds it again.</p></article>
  </div>
</section>

<section class="band" id="packs">
  <h2>All {c['findings']} packs</h2>
  <p class="hint">Each page shows the finding as the scan wrote it, the work pack, the fix and how it was judged.</p>
  {pack_rows(packs)}
</section>

<section class="band">
  <h2>What this replay doesn't show</h2>
  <ul class="plain">
    <li>One repo on one night. The same scan covered 31 repos: the entire Argus suite, its PyPI package repos, UI repos,
    infrastructure-as-code repos and the auditor itself.</li>
    <li>Review. A person read the PR, one commit per fix, and merged it: <a href="{e(run['pr']['url'])}">argus-scout#{run['pr']['number']}</a>.
    Nothing the auditor or an agent does merges by itself.</li>
    <li>Findings the scan missed. A replay can only show what was reported.</li>
  </ul>
</section>
"""
    return page(f"{SITE}: one night of ai-code-auditor, replayed",
                f"A recorded run of ai-code-auditor on argus-scout: {c['findings']} findings, the work packs they became, "
                "and the evidence that resolved each one.", body)


# ── a pack page ───────────────────────────────────────────────────────────────

def diff_html(diff: str) -> str:
    out = []
    for line in diff.splitlines():
        cls = ("meta" if line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file"))
               else "hunk" if line.startswith("@@") else "add" if line.startswith("+")
               else "del" if line.startswith("-") else "ctx")
        out.append(f'<span class="d-{cls}">{e(line) or " "}</span>')
    return '<pre class="diff"><code>' + "".join(out) + "</code></pre>"  # each line is a block span


def validation(p: dict, run: dict) -> str:
    ev, o = p["evidence"], p["outcome"]
    if o == "wontfix":
        return (f'<p>The agent checked the finding against the code and found it wrong. No fix, no test run: the pack '
                f'was closed with this note, which a person can read and overturn.</p><blockquote class="note">{md(p["note"])}</blockquote>')
    head = f'<p class="verdict verdict-{o}">{chip(o)} {e(ev["reason"])}</p>'
    meta = f'<p class="hint">Judged {e(when(ev["at"]))} against <code>dev</code> at {e(ev["base_sha"][:7])}.</p>'
    if o == "repro":
        proved = [t for t in ev["tests"] if t["repro"]]
        rest = [t for t in ev["tests"] if not t["repro"]]
        row = lambda t: (f'<tr class="{"hit" if t["repro"] else ""}"><td><code>{e(t["id"])}</code></td>'
                         f'<td class="r r-{e(t["dev"])}">{e(t["dev"])}</td><td class="r r-{e(t["patched"])}">{e(t["patched"])}</td></tr>')
        table = ('<div class="table-scroll"><table class="tests"><thead><tr><th scope="col">Test</th><th scope="col">on dev</th>'
                 f'<th scope="col">dev + patch</th></tr></thead><tbody>{"".join(row(t) for t in proved)}</tbody></table></div>')
        others = ""
        if rest:
            same = sum(1 for t in rest if t["dev"] == t["patched"])
            others = (f'<details class="more"><summary>The other {len(rest)} tests in the run ({same} with the same result on both trees)</summary>'
                      f'<div class="table-scroll"><table class="tests"><tbody>{"".join(row(t) for t in rest)}</tbody></table></div></details>')
        return head + meta + table + others
    if o == "waiver":
        return (head + meta + '<p>This fix strengthens a test. The stronger test passes on <code>dev</code>\'s code as '
                'well, so there is no failure on <code>dev</code> for it to show. It resolved on a written waiver of at least '
                'twenty characters, recorded with the pack:</p>' + f'<blockquote class="note">{md(ev["waiver"])}</blockquote>')
    return head + meta + ('<p>The patch only changes tests. They ran and passed, which is all a test-only change can '
                          'show, so the pack resolved as <code>test_only</code>.</p>')


def history_html(p: dict) -> str:
    items = "".join(f'<li><time datetime="{e(h["at"])}">{e(when(h["at"]))}</time> <strong>{e(h["status"])}</strong></li>'
                    for h in p["history"])
    return f'<ol class="history">{items}</ol>'


def pack_page(p: dict, prev: dict | None, nxt: dict | None, tape: dict) -> str:
    run = tape["run"]
    src = f'{run["repo_url"]}/blob/{run["scanned_sha"]}/{p["file"]}#L{p["line"]}' if p["line"] else ""
    crit = "".join(f"<li>{md(c)}</li>" for c in p["acceptance_criteria"])
    pager = ('<nav class="pager" aria-label="Packs">'
             + (f'<a href="../{e(prev["id"])}/" rel="prev">← {e(prev["title"][:60])}</a>' if prev else "<span></span>")
             + (f'<a href="../{e(nxt["id"])}/" rel="next">{e(nxt["title"][:60])} →</a>' if nxt else "<span></span>")
             + "</nav>")
    fix = ""
    if p["commit"]:
        cm = p["commit"]
        cut = f'<p class="hint">{cm["diff_cut"]} more lines on GitHub.</p>' if cm["diff_cut"] else ""
        shared = sum(1 for q in tape["packs"] if q["commit"] and q["commit"]["sha"] == cm["sha"])
        also = f' This commit closes {shared} packs.' if shared > 1 else ""
        label = "The fix" if p["outcome"] != "wontfix" else "A related change in the same PR"
        fix = (f'<section class="step" id="fix"><h2>{label}</h2><p><a href="{e(cm["url"])}"><code>{e(cm["sha"][:10])}</code></a> '
               f'{e(cm["subject"])}.{also}</p>{diff_html(cm["diff"])}{cut}</section>')
    body = f"""
<p class="crumbs"><a href="../../#packs">← All {tape['counts']['findings']} packs</a></p>
<article class="pack">
  <header class="pack-head">
    {chip(p['outcome'])}
    <h1>{md(p['title'])}</h1>
    <p class="pack-meta"><span>{e(p['finding_type'])}</span><span>{e(p['severity'].lower())}</span>
    <span>{f'<a href="{e(src)}"><code>{e(p["file"])}:{e(p["line"])}</code></a>' if src else f'<code>{e(p["file"])}</code>'}</span></p>
  </header>
  <section class="step"><h2>The finding</h2><p>{md(p['description'])}</p>
  <pre class="code"><code>{e(p['snippet'])}</code></pre></section>
  <section class="step"><h2>The work pack</h2><p><strong>Recommendation.</strong> {md(p['recommendation'])}</p>
  <p><strong>Acceptance criteria</strong></p><ul>{crit}</ul></section>
  <section class="step"><h2>Status</h2>{history_html(p)}</section>
  {fix}
  <section class="step" id="judged"><h2>How it was judged</h2>{validation(p, run)}</section>
</article>
{pager}
"""
    return page(f"{p['title']} · {SITE}", f"{SHORT[p['outcome']]}: {p['title']}", body, depth=2)


def build(out: Path, tape: dict) -> None:
    denylist_path = os.getenv("AI_AUDIT_OPSEC_DENYLIST", "")
    denylist = []
    if denylist_path and Path(denylist_path).is_file():
        from wiki.public import load_denylist
        denylist = load_denylist(denylist_path)
    check_public(tape, denylist, [])

    if out.exists():
        shutil.rmtree(out)
    (out / "vendor" / "dh").mkdir(parents=True)
    for f in (*DH_FILES, "dh-mark.svg"):
        shutil.copy2(HERE / "vendor" / "dh" / f, out / "vendor" / "dh" / f)
    for f in ("demo.css", "demo.js"):
        shutil.copy2(HERE / f, out / f)
    (out / "index.html").write_text(index_page(tape), encoding="utf-8")
    packs = tape["packs"]
    for i, p in enumerate(packs):
        d = out / "packs" / p["id"]
        d.mkdir(parents=True)
        (d / "index.html").write_text(pack_page(p, packs[i - 1] if i else None, packs[i + 1] if i + 1 < len(packs) else None, tape),
                                      encoding="utf-8")
    (out / "404.html").write_text(page(f"Not found · {SITE}", "No such page.",
                                       '<section class="hero"><h1>No such page</h1><p class="lede"><a href="/">Back to the run</a></p></section>'),
                                  encoding="utf-8")
    (out / "_headers").write_text(
        "/*\n"
        "  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: strict-origin-when-cross-origin\n"
        "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n",
        encoding="utf-8")
    for f in out.rglob("*.html"):  # a page with inline style or script would break under the CSP
        text = f.read_text(encoding="utf-8")
        if re.search(r"<style|style=\"|<script>(?!</script>)|<script(?![^>]*\bsrc=)", text):
            raise SystemExit(f"{f.relative_to(out)} has inline style or script")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"built {out} ({len(packs)} pack pages, {size / 1e3:.0f} KB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=HERE / "out")
    ap.add_argument("--tape", type=Path, default=TAPE)
    args = ap.parse_args()
    build(args.out, json.loads(args.tape.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
