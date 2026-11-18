"""Parse unified diffs (git format or plain diff -u) into structured objects."""
from __future__ import annotations

import codecs
import re

from auditor.contracts import DiffHunk, FileDiff


class _FileState:
    """Mutable parsing accumulator; converts to FileDiff when complete."""

    __slots__ = (
        "old_path", "new_path", "hunks",
        "is_new_file", "is_deleted", "is_rename",
        "_hunk", "_hunk_lines", "_old_line", "_new_line",
        "_old_left", "_new_left", "_saw_old_header",
    )

    def __init__(self) -> None:
        self.old_path = ""
        self.new_path = ""
        self.hunks: list[DiffHunk] = []
        self.is_new_file = False
        self.is_deleted = False
        self.is_rename = False
        self._hunk: DiffHunk | None = None
        self._hunk_lines: list[str] = []
        self._old_line = 0
        self._new_line = 0
        # Lines still owed to the open hunk, from its "@@ -a,b +c,d @@" counts.
        self._old_left = 0
        self._new_left = 0
        self._saw_old_header = False

    @property
    def canonical_path(self) -> str:
        return self.new_path if not self.is_deleted else self.old_path

    def close_hunk(self) -> None:
        if self._hunk is not None:
            self._hunk.raw_hunk = "\n".join(self._hunk_lines)
            self._hunk = None
            self._hunk_lines = []
        self._old_left = 0
        self._new_left = 0

    def in_hunk_body(self) -> bool:
        """True while the open hunk still expects body lines (per its header counts)."""
        return self._hunk is not None and (self._old_left > 0 or self._new_left > 0)

    def open_hunk(self, line: str) -> None:
        self.close_hunk()
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if not m:
            return
        h = DiffHunk(
            file_path=self.canonical_path,
            old_start=int(m.group(1)),
            old_count=int(m.group(2) or "1"),
            new_start=int(m.group(3)),
            new_count=int(m.group(4) or "1"),
            raw_hunk="",
            added_lines=[],
            removed_lines=[],
        )
        self._old_line = h.old_start
        self._new_line = h.new_start
        self._old_left = h.old_count
        self._new_left = h.new_count
        self._hunk = h
        self._hunk_lines = [line]
        self.hunks.append(h)

    def feed_line(self, line: str) -> None:
        if self._hunk is None:
            return
        self._hunk_lines.append(line)
        # Body lines are classified by their first character only: "--- x" in a
        # hunk is a removed "-- x" line, not a file header.
        if line.startswith("+"):
            self._hunk.added_lines.append(self._new_line)
            self._new_line += 1
            self._new_left -= 1
        elif line.startswith("-"):
            self._hunk.removed_lines.append(self._old_line)
            self._old_line += 1
            self._old_left -= 1
        elif line.startswith(" ") or line == "":
            # Context line (space-prefixed; tolerate empty lines from patch
            # tools that strip trailing whitespace). "\ No newline at end of
            # file" markers (and any other prefix) are ignored.
            self._old_line += 1
            self._new_line += 1
            self._old_left -= 1
            self._new_left -= 1

    def to_file_diff(self) -> FileDiff:
        self.close_hunk()
        return FileDiff(
            old_path=self.old_path,
            new_path=self.new_path,
            hunks=self.hunks,
            is_new_file=self.is_new_file,
            is_deleted=self.is_deleted,
            is_rename=self.is_rename,
        )


def _header_path(raw: str, prefix: str) -> str:
    """Path from a '--- '/'+++ ' header: drop a tab-separated timestamp, unquote
    git's C-style quoted paths, and strip the a/ or b/ prefix."""
    path = raw.split("\t", 1)[0]
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        try:
            path = codecs.escape_decode(path[1:-1].encode("utf-8"))[0].decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            path = path[1:-1]
    if path == "/dev/null":
        return ""
    if path.startswith(prefix):
        path = path[len(prefix):]
    return path


_BODY_PREFIXES = ("+", "-", " ", "\\")


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff string into a list of FileDiff objects.

    Supports both git diff format (with ``diff --git`` header) and plain
    ``diff -u`` format (starting directly with ``--- ``/``+++ ``).
    Binary files and empty diffs are silently skipped.
    """
    files: list[FileDiff] = []
    state: _FileState | None = None

    for line in diff_text.splitlines():
        # Inside a hunk body (per the header's line counts) every +/-/space line
        # belongs to the hunk, even one that looks like a "--- "/"+++ " header.
        if state is not None and state.in_hunk_body() and (line == "" or line.startswith(_BODY_PREFIXES)):
            state.feed_line(line)
            continue

        if line.startswith("diff --git "):
            if state is not None:
                files.append(state.to_file_diff())
            state = _FileState()
            m = re.match(r"^diff --git a/(.*) b/(.*)$", line)
            if m:
                state.old_path = m.group(1)
                state.new_path = m.group(2)
            continue

        # Non-git format: start a new file on the first "--- " line
        if state is None:
            if line.startswith("--- "):
                state = _FileState()
            else:
                continue

        if line.startswith("new file mode"):
            state.is_new_file = True
        elif line.startswith("deleted file mode"):
            state.is_deleted = True
        elif line.startswith("rename from") or line.startswith("rename to"):
            state.is_rename = True
        elif line.startswith("Binary files"):
            pass  # binary diff — no hunks
        elif line.startswith("--- "):
            if state.hunks or state._saw_old_header:
                # Plain multi-file `diff -u`: a second "--- " header starts the next file.
                files.append(state.to_file_diff())
                state = _FileState()
            state._saw_old_header = True
            state.old_path = _header_path(line[4:], "a/")
        elif line.startswith("+++ "):
            state.new_path = _header_path(line[4:], "b/")
        elif line.startswith("@@ "):
            state.open_hunk(line)
        else:
            state.feed_line(line)

    if state is not None:
        files.append(state.to_file_diff())

    return [f for f in files if f.old_path or f.new_path]


# ── Helpers used by diff_auditor ──────────────────────────────────────────────

def changed_files(file_diffs: list[FileDiff]) -> list[str]:
    """Return unique repo-relative paths of all files touched by the diff.

    Both sides of renames are included so downstream import analysis is complete.
    """
    seen: set[str] = set()
    result: list[str] = []
    for fd in file_diffs:
        for p in (fd.new_path, fd.old_path):
            if p and p not in seen:
                seen.add(p)
                result.append(p)
    return result


def hunk_new_range(hunk: DiffHunk) -> tuple[int, int]:
    """Return (start, end) 1-indexed line range in the new file covered by this hunk.

    Pure deletion hunks (``new_count == 0``) return an empty range
    (``new_start, new_start - 1``) so overlap checks don't spuriously match a
    CodeUnit whose start_line equals the hunk's new_start.
    """
    if hunk.new_count == 0:
        return hunk.new_start, hunk.new_start - 1
    return hunk.new_start, hunk.new_start + hunk.new_count - 1
