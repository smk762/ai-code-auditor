"""evidence: a generic-stem module imported in a wrapped, parenthesized list still ties (batch 5)."""
from __future__ import annotations

from pathlib import Path

import pytest

from auditor import evidence


def _pack_at(path: str) -> dict:
    return {"location": {"file_path": path}, "narrative": {"finding_type": "bug"}, "kind": "nightly"}


@pytest.mark.parametrize("src, ties", [
    ("from app.repair import (\n    other,\n    models,\n)\n", True),
    ("from app.repair import (models)\n", True),
    ("from app.repair import models\n", True),
    ("from app.repair import (\n    other,  # models live elsewhere\n)\n", False),
    ("from app.other import (\n    models,\n)\n", False),
], ids=["wrapped", "one_line_parens", "plain", "only_in_a_comment", "other_package"])
def test_wrapped_import_of_a_generic_stem(tmp_path: Path, src: str, ties: bool) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text(src + "\n\ndef test_c():\n    pass\n", encoding="utf-8")
    tid = "tests/test_m.py::test_c"
    repro = evidence.ReproResult(tool="pytest", patched={tid: "passed"}, base={tid: "failed"})
    got = evidence.for_pack(repro, _pack_at("app/repair/models.py"), tmp_path).repro
    assert got == ([tid] if ties else [])
