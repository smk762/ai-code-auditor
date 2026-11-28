"""The published docs (README.md and docs/*.md) can be imported by argus-handbook as they are.

The handbook (docs.dragonhound.dev) imports each package's README and ``docs/*.md`` from its ``dev`` branch and
refuses the whole import if any file fails its opsec gate. Its renderer titles a page by its first ``#`` heading,
drops raw HTML, and checks that every in-site link lands on a page and a heading. These tests hold this repo to the
same rules, so a docs change can't break the handbook's import (or a reader following a link on GitHub).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import update_readme_cli as gen

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1\s*$", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+\1", re.DOTALL)
LINK = re.compile(
    r"!?\[(?:[^\]\[]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"
)

# The handbook's opsec rules (argus-handbook web/scripts/opsec.mjs), minus its private denylist.
IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
OPSEC = {
    "private-ipv6": re.compile(
        r"\bf[cd][0-9a-f]{2}:[0-9a-f:]*:[0-9a-f]+\b", re.IGNORECASE
    ),
    "link-local-ipv6": re.compile(r"\bfe80:[0-9a-f:]*\b", re.IGNORECASE),
    "home-path": re.compile(
        r"(?:^|[\s\"'`(=])(/home/[A-Za-z0-9._-]+|/Users/[A-Za-z0-9._-]+)"
    ),
    "server-path": re.compile(
        r"(?:^|[\s\"'`(=])(/(?:mnt|media|srv|volume\d+)/[A-Za-z0-9._-]+)"
    ),
}
EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b"
)
RESERVED_EMAIL = re.compile(
    r"(?:^|\.)(?:example\.(?:com|net|org)|example|test|invalid|localhost)$",
    re.IGNORECASE,
)

# The README's generated command table sits between two HTML comments (scripts/update_readme_cli.py). The handbook
# renders with react-markdown's `skipHtml` (argus-handbook web/src/components/docs/PackageDoc.tsx), which drops a
# comment and leaves the table between them, so these two exact markers in README.md are the only raw HTML allowed.
CLI_MARKERS = (gen.START, gen.END)


def without_cli_markers(doc: Path) -> str:
    """The doc's text, minus the generated-table markers when it is README.md (and only there)."""
    text = doc.read_text()
    if doc == ROOT / "README.md":
        for marker in CLI_MARKERS:
            text = text.replace(marker, "")
    return text


def _private_ipv4(a: int, b: int) -> bool:
    return (
        a == 10
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
        or (a == 100 and 64 <= b <= 127)
        or (a == 169 and b == 254)
    )


def prose(text: str) -> str:
    """The markdown with fenced code blocks and code spans blanked out."""
    return CODE_SPAN.sub("", FENCE.sub("", text))


def slugs(text: str) -> set[str]:
    """GitHub's heading ids for a markdown file (what the handbook's renderer gives them too)."""
    seen: dict[str, int] = {}
    out = set()
    for line in FENCE.sub("", text).splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1)).replace(
            "`", ""
        )
        base = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(base, 0)
        seen[base] = count + 1
        out.add(base if count == 0 else f"{base}-{count}")
    return out


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_doc_has_a_title(doc: Path):
    first = next(
        (
            line
            for line in FENCE.sub("", doc.read_text()).splitlines()
            if line.startswith("#")
        ),
        "",
    )
    assert first.startswith("# "), "the handbook titles a page by its first `#` heading"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_doc_passes_the_opsec_gate(doc: Path):
    found = []
    for number, line in enumerate(doc.read_text().splitlines(), 1):
        for match in IPV4.finditer(line):
            octets = [int(o) for o in match.groups()]
            if all(o <= 255 for o in octets) and _private_ipv4(octets[0], octets[1]):
                found.append(f"line {number}: private address {match.group(0)}")
        for rule, pattern in OPSEC.items():
            found += [
                f"line {number}: {rule} {m.group(m.lastindex or 0).strip()}"
                for m in pattern.finditer(line)
            ]
        found += [
            f"line {number}: email {m.group(0)}"
            for m in EMAIL.finditer(line)
            if not RESERVED_EMAIL.search(m.group(1))
        ]
    assert not found, (
        "use placeholders such as `<GPU_HOST>` or container paths:\n" + "\n".join(found)
    )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_doc_has_no_raw_html(doc: Path):
    """The handbook renders markdown with raw HTML skipped, so a bare ``<PLACEHOLDER>`` would vanish there."""
    tags = re.findall(r"<[A-Za-z/!][^>\n]*>", prose(without_cli_markers(doc)))
    assert not tags, f"put these in backticks: {tags}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(doc: Path):
    broken = []
    for target in LINK.findall(prose(doc.read_text())):
        if re.match(r"^[a-z][a-z0-9+.-]*:|^//", target, re.IGNORECASE):
            continue
        path, _, anchor = target.partition("#")
        dest = (doc.parent / path).resolve() if path else doc
        if not dest.exists():
            broken.append(f"{target}: no such file")
        elif anchor and dest.suffix == ".md" and anchor not in slugs(dest.read_text()):
            broken.append(f"{target}: no heading with id {anchor!r}")
    assert not broken, "\n".join(broken)


def test_every_guide_is_linked_from_the_readme():
    readme = (ROOT / "README.md").read_text()
    missing = [p.name for p in DOCS[1:] if f"(docs/{p.name})" not in readme]
    assert not missing, f"link these from the README's Docs table: {missing}"
