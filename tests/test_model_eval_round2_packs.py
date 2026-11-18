"""Regression test for the round-1 unit match in scripts/model_eval_round2.py (no model calls)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("model_eval_round2", ROOT / "scripts" / "model_eval_round2.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_unit_edited_in_place_is_not_reused() -> None:
    from auditor.contracts import CodeUnit

    r2 = _load()
    unit = CodeUnit("u1", "r", "a.py", "f", "function", "python", 10, 20, raw_text="new body", raw_text_hash="new")
    same = {"start_line": 10, "end_line": 20, "raw_text_hash": "new"}
    edited = {"start_line": 10, "end_line": 20, "raw_text_hash": "old"}  # same span, different code
    moved = {"start_line": 11, "end_line": 21, "raw_text_hash": "new"}
    assert r2.unchanged(unit, same)
    assert not r2.unchanged(unit, edited)
    assert not r2.unchanged(unit, moved)
    assert not r2.unchanged(unit, None)
