"""Regression tests for wiki/build.py audit packs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki import build as build_mod


def test_top_index_survives_a_write_that_dies_halfway(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "wiki"
    (root / "svc").mkdir(parents=True)
    (root / "svc" / "INDEX.json").write_text(json.dumps({"repo": "svc", "pages": [{"slug": "overview"}]}))
    build_mod._write_top_index(tmp_path)
    before = (root / "INDEX.json").read_text()

    real = Path.write_text

    def dies_halfway(self, data, *a, **kw):  # a crash / full disk mid-write
        real(self, data[: len(data) // 2], *a, **kw)
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", dies_halfway)
    with pytest.raises(OSError):
        build_mod._write_top_index(tmp_path)
    monkeypatch.undo()

    assert (root / "INDEX.json").read_text() == before  # readers still see the whole old index
    assert json.loads(before)["repos"]["svc"]["pages"] == 1
    assert sorted(p.name for p in root.iterdir()) == ["INDEX.json", "svc"]  # no temp file left behind
