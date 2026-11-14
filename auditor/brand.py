"""Argus brand marks (argus-cortex ``brand/``) for the UI, served from our own origin.

Each package mark is a 24-unit SVG with one coloured eye showing the package's pipeline stage
(``brand/README.md`` in argus-cortex). They are read from the local argus-cortex mirror when it
has ``brand/``; otherwise fetched once from GitHub (``dev``) and cached under
``<output_dir>/_brand/``. raw.githubusercontent.com serves SVG as text/plain with nosniff, so
pages can't use those URLs directly.
"""
from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
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
    "cull": "data", "lightbox": "data",
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


def _safe(svg: bytes) -> bool:
    head = svg.lstrip()[:200].lower()
    return (head.startswith(b"<svg") or head.startswith(b"<?xml")) and b"<script" not in svg.lower() \
        and not re.search(rb"\son\w+\s*=", svg, re.IGNORECASE) and len(svg) <= MAX_SVG_BYTES


def _mirror_dir() -> Path | None:
    from auditor.config import load_ecosystem_config

    try:
        repo = next((r for r in load_ecosystem_config().repos if r.name == CORTEX), None)
    except Exception:  # noqa: BLE001
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
        return cache.read_bytes()
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
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data
