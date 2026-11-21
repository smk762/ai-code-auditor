"""chunker: the <module> unit's stitched text maps back to its source lines."""
from __future__ import annotations

from pathlib import Path

from auditor.chunker import extract_code_units
from auditor.llm_client import _heuristic_findings

SRC = """import os

DEBUG = True

def handler():
    return 1

def other():
    return 2

if __name__ == "__main__":
    API_KEY = "sk-live-0123456789"
    a = 1; b = 2
"""


def _module_unit(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text(SRC)
    return next(u for u in extract_code_units("r", f) if u.symbol == "<module>")


def test_every_module_text_line_maps_to_the_same_source_line(tmp_path: Path) -> None:
    from auditor.chunker import source_line

    unit = _module_unit(tmp_path)
    source = SRC.split("\n")
    for offset, text in enumerate(unit.raw_text.split("\n")):
        assert source[source_line(unit, offset) - 1] == text


def test_statements_sharing_a_line_emit_it_once(tmp_path: Path) -> None:
    unit = _module_unit(tmp_path)
    assert unit.raw_text.count("a = 1; b = 2") == 1


def test_heuristic_finding_in_a_late_module_block_cites_its_real_line(tmp_path: Path) -> None:
    unit = _module_unit(tmp_path)
    secret = [f for f in _heuristic_findings(unit) if f.title == "Potential hardcoded secret"]
    assert secret and secret[0].line == SRC.split("\n").index('    API_KEY = "sk-live-0123456789"') + 1


def test_contiguous_units_map_from_start_line(tmp_path: Path) -> None:
    from auditor.chunker import source_line

    f = tmp_path / "app.py"
    f.write_text(SRC)
    fn = next(u for u in extract_code_units("r", f) if u.symbol == "other")
    assert source_line(fn, 1) == fn.start_line + 1
