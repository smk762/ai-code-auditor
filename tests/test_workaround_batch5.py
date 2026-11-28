"""workaround: explicit paths obey the walk's exclusions, and C-family block comments are read
wherever they start (batch 5 packs 0c5710734e, 1240625871)."""
from __future__ import annotations

from pathlib import Path

from auditor import workaround as w

HACK_JS = "// HACK: the API drops the header, re-add it\nexport const x = 1;\n"


def test_explicit_paths_skip_what_a_full_walk_skips(tmp_path: Path) -> None:
    for rel in ("app/main.js", "app/lib.min.js", "app/types.d.ts", "node_modules/pkg/i.js", ".cache/x.js",
                "vendor/v.js"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(HACK_JS)
    full = sorted(f.file_path for f in w.scan("r", tmp_path))
    explicit = sorted(f.file_path for f in w.scan("r", tmp_path, ["app/main.js", "app/lib.min.js", "app/types.d.ts",
                                                                   "node_modules/pkg/i.js", ".cache/x.js",
                                                                   "vendor/v.js"]))
    assert explicit == full == [str(tmp_path / "app/main.js")]


def test_a_trailing_block_comment_is_read() -> None:
    assert w.comments("a.js", "foo(); /* HACK: the API drops the header */\n") == [
        (1, "HACK: the API drops the header")]


def test_a_block_comment_opened_after_code_continues_on_the_next_lines() -> None:
    text = "int x = 1; /* see below\n * workaround for the driver bug\n */ y();\n"
    got = w.comments("a.ts", text)
    assert (2, "workaround for the driver bug") in got
    assert w.classify(dict(got)[2], text.splitlines(), 2) == "marker"


def test_a_line_comment_after_a_closed_block_is_read_and_strings_are_not() -> None:
    text = 'a(); /* ok */ b(); // HACK: retry twice\nconst s = "/* HACK */";\n'
    assert w.comments("a.js", text) == [(1, "ok HACK: retry twice")]


def test_leading_block_and_line_comments_read_as_before() -> None:
    text = "/**\n * HACK: x\n */\n// FIXME: y\nconst u = 'http://h';\n"
    assert w.comments("a.js", text) == [(2, "HACK: x"), (4, "FIXME: y")]
