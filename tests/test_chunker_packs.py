"""Regression tests for auditor/chunker.py Python unit extraction."""
from __future__ import annotations

import ast

import pytest

from auditor import chunker


def _units(tmp_path, src: str, name: str = "m.py"):
    f = tmp_path / name
    f.write_bytes(src.encode("utf-8"))
    return chunker.extract_code_units("r", f)


def test_decorators_are_part_of_the_unit(tmp_path):
    src = (
        "import x\n"
        "\n"
        "@app.get('/admin')\n"
        "@requires_auth\n"
        "def admin():\n"
        "    return 1\n"
    )
    (u,) = [u for u in _units(tmp_path, src) if u.symbol == "admin"]
    assert u.start_line == 3 and u.end_line == 6
    assert u.raw_text.startswith("@app.get('/admin')\n@requires_auth\ndef admin():")


def test_module_level_code_is_its_own_unit(tmp_path):
    src = (
        '"""Doc."""\n'
        "import os\n"
        "API_KEY = 'sk-live-123'\n"
        "\n"
        "def f():\n"
        "    return 1\n"
        "\n"
        "app.include_router(r)\n"
        "if __name__ == '__main__':\n"
        "    os.system('x')\n"
    )
    units = _units(tmp_path, src)
    mod = [u for u in units if u.kind == "module"]
    assert len(mod) == 1
    m = mod[0]
    assert (m.start_line, m.end_line) == (3, 10)
    assert "API_KEY = 'sk-live-123'" in m.raw_text
    assert "app.include_router(r)" in m.raw_text
    assert "os.system('x')" in m.raw_text
    assert "def f" not in m.raw_text and "import os" not in m.raw_text and "Doc." not in m.raw_text


def test_no_module_unit_for_imports_and_docstring_only(tmp_path):
    src = '"""Doc."""\nimport os\n\ndef f():\n    return 1\n'
    assert [u.kind for u in _units(tmp_path, src)] == ["function"]


def test_line_numbers_survive_form_feeds_and_unicode_separators(tmp_path):
    src = (
        "# a\x0cb\n"            # form feed: not a line break for the AST
        "s = 'x\u2028y'\n"      # line separator inside a string
        "def g():\n"
        "    return 2\n"
    )
    (g,) = [u for u in _units(tmp_path, src) if u.symbol == "g"]
    assert (g.start_line, g.end_line) == (3, 4)
    assert g.raw_text == "def g():\n    return 2"


@pytest.mark.parametrize("exc", [ValueError, RecursionError, MemoryError])
def test_parse_errors_fall_back_to_whole_file(tmp_path, monkeypatch, exc):
    real_parse = ast.parse

    def parse(source, filename="<unknown>", *a, **k):
        if str(filename).endswith("bad.py"):
            raise exc("boom")
        return real_parse(source, filename, *a, **k)

    monkeypatch.setattr(chunker.ast, "parse", parse)
    (u,) = _units(tmp_path, "def f():\n    return 1\n", name="bad.py")
    assert u.kind == "module" and u.start_line == 1
