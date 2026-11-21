"""ecosystem.api_mapper: dotted paths kept whole, templates and test fixtures never become endpoints."""
from __future__ import annotations

from pathlib import Path

from ecosystem.api_mapper import discover_api_endpoints


def _src(tmp_path: Path, text: str, name: str = "routes.py") -> list[str]:
    f = tmp_path / name
    f.write_text(text)
    return discover_api_endpoints(f)


def test_paths_with_a_dot_are_not_truncated(tmp_path):
    text = ('"""Serves GET /audit/findings.json and GET /audit/brand/packages/{package}.svg."""\n'
            '# see GET /audit/findings, then POST /audit/run.\n')
    assert _src(tmp_path, text) == ["GET /audit/brand/packages/{package}.svg", "GET /audit/findings",
                                    "GET /audit/findings.json", "POST /audit/run"]


def test_format_templates_are_not_endpoints(tmp_path):
    text = ('url = f"GET /api/v1/resource_{i:04d}"\nother = "GET /x/{0}"\n'
            '@app.get("/files/{file_path:path}")\n@router.get("/items/{item_id}")\n')
    assert _src(tmp_path, text) == ["GET /files/{file_path:path}", "GET /items/{item_id}"]


def test_test_modules_and_conftest_are_skipped(tmp_path):
    for name in ("test_api.py", "api_test.py", "conftest.py"):
        assert _src(tmp_path, '@app.get("/alpha")\nr = "GET /admin"\n', name) == []
    assert _src(tmp_path, '@app.get("/alpha")\n', "api.py") == ["GET /alpha"]
