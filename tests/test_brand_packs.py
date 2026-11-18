"""Regression tests for auditor/brand.py (SVG allowlist, cache validation, cache write failures)."""
from __future__ import annotations

from pathlib import Path

import pytest

from auditor import brand

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="2" fill="#b33a2a"/></svg>'


@pytest.mark.parametrize(
    "payload",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(document.domain)"><rect width="9"/></a></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="href" values="javascript:alert(1)"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><iframe src="javascript:alert(1)"/></foreignObject></svg>',
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "&#60;script>alert(1)&#60;/script>">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<use xlink:href="data:image/svg+xml;base64,PHN2Zz4="/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://evil.test/x.css)</style></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(https://evil.test/#x)"/></svg>',
    ],
)
def test_active_or_external_svg_content_is_rejected(payload):
    assert not brand._safe(payload)


def test_plain_mark_is_accepted():
    assert brand._safe(SVG)
    assert brand._safe(b'<?xml version="1.0"?>\n' + SVG)


@pytest.fixture
def no_mirror(monkeypatch):
    monkeypatch.setattr(brand, "_mirror_dir", lambda: None)


class _Resp:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        return self.data


def test_bad_cached_mark_is_not_served_and_is_refetched(tmp_path: Path, no_mirror, monkeypatch):
    cache = tmp_path / "_brand" / "argus-forge-light.svg"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><rect')  # truncated by a crashed write
    monkeypatch.setattr(brand.urllib.request, "urlopen", lambda req, timeout: _Resp(SVG))
    assert brand.mark_svg(tmp_path, "forge", "light") == SVG
    assert cache.read_bytes() == SVG
    assert not list(cache.parent.glob("*.tmp"))


def test_unwritable_cache_still_returns_the_fetched_mark(tmp_path: Path, no_mirror, monkeypatch):
    monkeypatch.setattr(brand.urllib.request, "urlopen", lambda req, timeout: _Resp(SVG))
    (tmp_path / "_brand").write_text("a file where the cache dir should be")
    assert brand.mark_svg(tmp_path, "forge", "dark") == SVG
