"""Argus brand marks (argus-cortex ``brand/``) for the UI, served from our own origin.

Each package mark is a 24-unit SVG with one coloured eye showing the package's pipeline stage
(``brand/README.md`` in argus-cortex). They are read from the local argus-cortex mirror when it
has ``brand/``; otherwise fetched once from GitHub (``dev``) and cached under
``<output_dir>/_brand/``. raw.githubusercontent.com serves SVG as text/plain with nosniff, so
pages can't use those URLs directly.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

CORTEX = "argus-cortex"
CORTEX_SLUG = "smk762/argus-cortex"
VARIANTS = ("light", "dark", "mono")
MAX_SVG_BYTES = 64_000

# stage → (label, light eye, dark eye), from brand/README.md in argus-cortex
STAGES: dict[str, tuple[str, str, str]] = {
    "data": ("Data", "#1d5f74", "#6fb7cc"),
    "training": ("Training", "#b33a2a", "#e57a64"),
    "apps": ("Apps", "#8f6210", "#e8b54e"),
    "foundation": ("Foundation", "#2a4a7a", "#8eacdc"),
}
# packages with a mark in brand/packages/ (short name → stage), from brand/README.md
MARKS: dict[str, str] = {
    "quarry": "data", "curator": "data", "lens": "data",
    "forge": "training", "proof": "training",
    "atlas": "apps", "darkroom": "apps", "mint": "apps", "survey": "apps", "mask": "apps",
    "cortex": "foundation", "studio": "foundation",
}
# argus repos without their own mark, placed by what they serve (demo UIs follow their package)
STAGE_OF_UNMARKED: dict[str, str] = {
    "cull": "data", "lightbox": "data", "puppet": "data",
    "safelight": "apps", "sightline": "apps", "assay": "apps",
    "ci": "foundation", "halo": "foundation", "pkg-template": "foundation",
}


def stage_of(repo: str) -> str | None:
    """Brand stage of an ecosystem repo; None for non-argus dependencies."""
    if not repo.startswith("argus-"):
        return None
    short = repo.removeprefix("argus-")
    return MARKS.get(short) or STAGE_OF_UNMARKED.get(short)


def has_mark(repo: str) -> bool:
    return repo.startswith("argus-") and repo.removeprefix("argus-") in MARKS


_SVG_NS = "http://www.w3.org/2000/svg"
# Marks are plain shapes. Anything that can link, animate, embed or carry styles/script is out.
_ALLOWED_TAGS = frozenset({
    "svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon", "title", "desc",
    "defs", "lineargradient", "radialgradient", "stop", "clippath", "symbol",
})
_ALLOWED_ATTRS = frozenset({
    "viewbox", "version", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "fx", "fy", "d", "points", "fill", "fill-rule", "fill-opacity", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset",
    "stroke-opacity", "opacity", "transform", "id", "class", "clip-path", "clip-rule", "offset",
    "stop-color", "stop-opacity", "gradientunits", "gradienttransform", "preserveaspectratio", "role",
    "aria-label", "aria-hidden", "focusable", "vector-effect", "shape-rendering",
})
# url(...) is only allowed as a same-document reference such as url(#grad).
_BAD_VALUE = re.compile(r"javascript:|data:|url\((?!\s*['\"]?\s*#)", re.IGNORECASE)
# The parser drops processing instructions (<?xml-stylesheet?> can pull in CSS or XSLT), so the
# allowlist walk never sees them; only a leading XML declaration may carry "<?".
_XML_DECL = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*<\?xml\s[^?]*\?>")


def _safe(svg: bytes) -> bool:
    """A plain SVG mark: parsed (never pattern-matched) and checked against an allowlist.

    DTDs, entities and processing instructions are refused outright, so nothing can expand into
    markup or pull in a stylesheet after the check."""
    if len(svg) > MAX_SVG_BYTES:
        return False
    lowered = svg.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return False
    if b"<?" in _XML_DECL.sub(b"", svg, count=1):
        return False
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return False
    for el in root.iter():
        if not isinstance(el.tag, str):
            return False
        ns, _, local = el.tag.rpartition("}")
        if ns and ns.lstrip("{") != _SVG_NS:
            return False
        if local.lower() not in _ALLOWED_TAGS:
            return False
        for name, value in el.attrib.items():
            if "}" in name or name.lower() not in _ALLOWED_ATTRS or _BAD_VALUE.search(value):
                return False
    return root.tag.rpartition("}")[2].lower() == "svg"


def _write_cache(cache: Path, data: bytes) -> None:
    """Best-effort atomic cache write: a temp file in the same dir, then os.replace."""
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=f".{cache.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, cache)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        logger.warning("brand mark cache %s not written: %s", cache, exc)


def _mirror_dir() -> Path | None:
    from auditor.config import load_ecosystem_config

    try:
        repo = next((r for r in load_ecosystem_config().repos if r.name == CORTEX), None)
    except Exception:  # noqa: BLE001, S110 — no config means no mirror; the mark falls back to its cache
        return None
    return Path(repo.path) / "brand" / "packages" if repo and repo.path else None


def mark_svg(output_dir: str | Path, package: str, variant: str = "light") -> bytes | None:
    """The mark SVG for a package short name (``forge``), or None if there isn't one."""
    if package not in MARKS or variant not in VARIANTS:
        return None
    name = f"argus-{package}-{variant}.svg"
    mirror = _mirror_dir()
    if mirror and (mirror / package / name).is_file():
        data = (mirror / package / name).read_bytes()
        return data if _safe(data) else None
    cache = Path(output_dir) / "_brand" / name
    if cache.is_file():
        try:
            cached = cache.read_bytes()
        except OSError:
            cached = b""
        if _safe(cached):
            return cached
        logger.warning("brand mark cache %s failed the check; refetching", cache)
        try:
            cache.unlink(missing_ok=True)
        except OSError:
            pass
    url = f"https://raw.githubusercontent.com/{CORTEX_SLUG}/dev/brand/packages/{package}/{name}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ai-code-auditor"}),
                                    timeout=10) as resp:
            data = resp.read(MAX_SVG_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("brand mark %s unavailable: %s", name, exc)
        return None
    if not _safe(data):
        logger.warning("brand mark %s rejected (not a plain SVG)", name)
        return None
    _write_cache(cache, data)
    return data
