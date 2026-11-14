#!/usr/bin/env python3
"""Build auditor/static/service-icons.json: one small icon per external service on the map.

Brand logos come from Simple Icons (CC0 SVG paths; the logos remain their owners' trademarks
and are used here only to name the service). Services Simple Icons doesn't cover get a generic
glyph in the map's own line style. Rerun after changing SIMPLE_ICONS_VERSION or the mapping:

    python scripts/build_service_icons.py
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

SIMPLE_ICONS_VERSION = "16.36.0"  # published 2026-10-25
OUT = Path(__file__).resolve().parents[1] / "auditor" / "static" / "service-icons.json"

# service id (auditor/ui_topology.py SERVICES) → Simple Icons slug
BRANDS = {
    "ollama": "ollama", "anthropic": "anthropic", "qdrant": "qdrant", "postgres": "postgresql",
    "redis": "redis", "s3": "minio", "ipfs": "ipfs", "ethereum": "ethereum",
    "huggingface": "huggingface", "geo": "gdal",
}
_STROKE = 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
GENERIC = {  # 24-unit glyphs for services without a brand icon
    "shield": f'<path {_STROKE} d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path {_STROKE} d="M9 12l2 2 4-4"/>',
    "braces": f'<path {_STROKE} d="M8 4C6 4 5 5 5 7v2c0 1-1 2-2 2 1 0 2 1 2 2v2c0 2 1 3 3 3M16 4c2 0 3 1 3 3v2c0 1 1 2 2 2-1 0-2 1-2 2v2c0 2-1 3-3 3"/>',
    "cube": f'<path {_STROKE} d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path {_STROKE} d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
    "bell": f'<path {_STROKE} d="M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z"/><path {_STROKE} d="M10 20a2 2 0 0 0 4 0"/>',
}
GENERIC_FOR = {"nudenet": "shield", "singguard": "shield", "openai": "braces", "diffusers": "cube",
               "trainers": "cube", "mithrandir": "bell"}


def brand_path(slug: str) -> str:
    url = f"https://cdn.jsdelivr.net/npm/simple-icons@{SIMPLE_ICONS_VERSION}/icons/{slug}.svg"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ai-code-auditor"}), timeout=20) as r:
        svg = r.read().decode("utf-8")
    m = re.search(r'<path d="([^"]+)"', svg)
    if not m or "<script" in svg:
        raise ValueError(f"unexpected SVG for {slug}")
    return m.group(1)


def main() -> int:
    icons: dict[str, dict[str, str]] = {}
    for sid, slug in BRANDS.items():
        icons[sid] = {"body": f'<path fill="currentColor" d="{brand_path(slug)}"/>',
                      "source": f"simple-icons {SIMPLE_ICONS_VERSION}: {slug}"}
    for sid, glyph in GENERIC_FOR.items():
        icons[sid] = {"body": GENERIC[glyph], "source": f"generic: {glyph}"}
    payload = {"_meta": {"viewBox": "0 0 24 24",
                         "license": "Brand paths from Simple Icons (CC0 1.0); logos are trademarks of their owners.",
                         "generated_by": "scripts/build_service_icons.py"},
               "icons": dict(sorted(icons.items()))}
    OUT.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(icons)} icons)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
