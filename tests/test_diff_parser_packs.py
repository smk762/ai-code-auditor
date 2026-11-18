"""Regression tests for auditor/diff_parser.py hunk state, multi-file plain diffs and header paths."""
from __future__ import annotations

from auditor.diff_parser import parse_diff

SQL_DIFF = """\
diff --git a/db/schema.sql b/db/schema.sql
index 111..222 100644
--- a/db/schema.sql
+++ b/db/schema.sql
@@ -1,4 +1,4 @@
 CREATE TABLE t (
--- old comment
+++ new comment
   id int
 );
"""


def test_header_like_body_lines_stay_in_the_hunk():
    files = parse_diff(SQL_DIFF)
    assert len(files) == 1
    fd = files[0]
    assert fd.old_path == "db/schema.sql"
    assert fd.new_path == "db/schema.sql"
    (h,) = fd.hunks
    assert h.removed_lines == [2]
    assert h.added_lines == [2]
    assert "--- old comment" in h.raw_hunk and "+++ new comment" in h.raw_hunk


def test_line_numbers_after_header_like_line_are_not_shifted():
    diff = """\
--- a/x.lua
+++ b/x.lua
@@ -10,3 +10,4 @@
 a
--- gone
+++i
+added
 b
"""
    (fd,) = parse_diff(diff)
    assert fd.old_path == "x.lua" and fd.new_path == "x.lua"
    (h,) = fd.hunks
    assert h.removed_lines == [11]
    assert h.added_lines == [11, 12]


def test_multi_file_plain_diff_gives_one_filediff_per_file():
    diff = """\
--- one.py\t2024-01-01 12:00:00.000000000 +0000
+++ one.py\t2024-01-02 12:00:00.000000000 +0000
@@ -1 +1 @@
-a
+b
--- two.py\t2024-01-01 12:00:00.000000000 +0000
+++ two.py\t2024-01-02 12:00:00.000000000 +0000
@@ -5,2 +5,2 @@
 x
-y
+z
"""
    files = parse_diff(diff)
    assert [f.new_path for f in files] == ["one.py", "two.py"]
    assert [f.old_path for f in files] == ["one.py", "two.py"]
    assert files[0].hunks[0].added_lines == [1]
    assert files[1].hunks[0].added_lines == [6]


def test_git_quoted_path_with_trailing_tab():
    diff = (
        'diff --git "a/dir/my file.py" "b/dir/my file.py"\n'
        "--- a/dir/my file.py\t\n"
        '+++ "b/dir/caf\\303\\251.py"\n'
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    (fd,) = parse_diff(diff)
    assert fd.old_path == "dir/my file.py"
    assert fd.new_path == "dir/café.py"


def test_git_multi_file_and_no_newline_marker_still_parse():
    diff = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x
+y
\\ No newline at end of file
diff --git a/b.py b/b.py
new file mode 100644
--- /dev/null
+++ b/b.py
@@ -0,0 +1,2 @@
+one
+two
"""
    files = parse_diff(diff)
    assert [f.new_path for f in files] == ["a.py", "b.py"]
    assert files[1].is_new_file and files[1].old_path == ""
    assert files[1].hunks[0].added_lines == [1, 2]
