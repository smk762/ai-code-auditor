"""Regression tests for wiki/sources.py audit packs."""
from pathlib import Path

from wiki import sources

PY = Path("m.py")
EMPTY = {"imports": [], "exports": None, "top_level": []}


def test_parse_value_error_returns_empty_structure(monkeypatch):
    # Python < 3.12 raises ValueError (not SyntaxError) for NUL bytes in source
    def boom(*_a, **_k):
        raise ValueError("source code string cannot contain null bytes")
    monkeypatch.setattr(sources.ast, "parse", boom)
    assert sources.file_structure(PY, "x = 1\x00") == EMPTY


def test_pathologically_nested_source_returns_empty_structure():
    assert sources.file_structure(PY, "a" + ".b" * 200_000) == EMPTY


def test_non_iterable_all_literal_is_dynamic():
    assert sources.file_structure(PY, "__all__ = None\n")["exports"] == [sources.DYNAMIC_ALL]
    assert sources.file_structure(PY, "__all__ = 3\n")["exports"] == [sources.DYNAMIC_ALL]


def test_annotated_all_is_read():
    assert sources.file_structure(PY, "__all__: list[str] = ['a', 'b']\n")["exports"] == ["a", "b"]


def test_augmented_all_extends_literal():
    assert sources.file_structure(PY, "__all__ = ['a']\n__all__ += ['b']\n")["exports"] == ["a", "b"]
    assert sources.file_structure(PY, "__all__ = []\n__all__ += ('c',)\n")["exports"] == ["c"]


def test_augmented_all_with_dynamic_value_is_dynamic():
    out = sources.file_structure(PY, "from x import y\n__all__ = ['a']\n__all__ += y.__all__\n")
    assert out["exports"] == [sources.DYNAMIC_ALL]


def test_bare_annotation_is_ignored():
    assert sources.file_structure(PY, "__all__: list[str]\n")["exports"] is None


def test_plain_all_unchanged():
    assert sources.file_structure(PY, "__all__ = ['x', 'y']\n")["exports"] == ["x", "y"]
