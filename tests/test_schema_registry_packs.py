from __future__ import annotations

from pathlib import Path

from ecosystem.schema_registry import discover_schemas


def test_non_utf8_schema_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "good_schema.json").write_text('{"type": "object"}', encoding="utf-8")
    (tmp_path / "bad_schema.json").write_bytes('{"title": "café"}'.encode("latin-1"))
    found = discover_schemas(str(tmp_path))
    assert list(found.values()) == [{"type": "object"}]
