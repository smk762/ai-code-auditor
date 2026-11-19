from __future__ import annotations

from pathlib import Path

from ecosystem.schema_registry import discover_schemas


def test_non_utf8_schema_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "good_schema.json").write_text('{"type": "object"}', encoding="utf-8")
    (tmp_path / "bad_schema.json").write_bytes('{"title": "café"}'.encode("latin-1"))
    found = discover_schemas(str(tmp_path))
    assert list(found.values()) == [{"type": "object"}]


def test_vendored_and_build_schemas_are_not_the_repos(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "order_schema.json").write_text('{"title": "order"}', encoding="utf-8")
    for d in ("node_modules/ajv/lib/refs", ".venv/lib/site-packages/jsonschema", ".git/x", "dist", "build"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "json-schema-draft-07.schema.json").write_text('{"title": "meta"}', encoding="utf-8")
    found = discover_schemas(str(tmp_path))
    assert found == {str(tmp_path / "api" / "order_schema.json"): {"title": "order"}}
