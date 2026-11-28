"""Workaround comments (#60): a comment that admits the code around it works around a problem.

Agents copy what is in the repo, and a workaround copied is a problem spread. This flags only
comments that *justify* a workaround, not comments in general:

* ``marker``: a comment opening with ``HACK``, ``KLUDGE``, ``FIXME`` or ``XXX``; ``workaround``
  (not "the workaround is gone"); ``temporary fix/hack``; ``TODO: remove …``; ``remove
  once/when …``; ``until`` an upstream bug, issue or release; ``compat shim``.
* ``race_timer``: a comment giving a race, flakiness or timing as the reason, with a sleep or
  retry call on its own line or the next two.

Each hit is a finding (source ``workaround``, class ``workaround``) pointing at the real fix:
remove the cause, then the workaround and its comment.

Comments only: Python is read with ``tokenize`` (so a string that mentions HACK never matches);
JS/TS and shell lines by their comment markers outside quotes. Vendored, generated and lock files
are skipped, and a line carrying ``workaround: ok`` is an accepted, permanent one.
"""
from __future__ import annotations

import io
import os
import re
import tokenize
from pathlib import Path

from auditor.contracts import Finding

SOURCE = "workaround"
FINDING_CLASS = "workaround"
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".sh", ".bash"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".mypy_cache",
             ".pytest_cache", "vendor", "third_party", ".next", "coverage", "site-packages"}
MAX_FILE_BYTES = 512_000
ACCEPTED = re.compile(r"workaround:\s*ok\b", re.IGNORECASE)

_MARKERS = [
    # upper-case markers opening the comment ("FIXME: …", "HACK - …"): a comment that merely
    # mentions the word is describing markers, not leaving one
    re.compile(r"^(HACK|KLUDGE|FIXME|XXX)\b"),
    re.compile(r"\bwork[- ]?arounds?\b", re.IGNORECASE),
    re.compile(r"\btemporar(y|ily)\b.{0,40}\b(fix|hack|patch|workaround|until|disable)", re.IGNORECASE),
    re.compile(r"\bTODO\b.{0,30}\bremove\b", re.IGNORECASE),
    re.compile(r"\bremove (this |it |me )?(once|when|after)\b", re.IGNORECASE),
    # waiting on someone else's fix, not describing this code's own behaviour
    re.compile(r"\buntil\b.{0,60}\b(upstream|bug|issue|#\d+|PR|new version|release of)\b", re.IGNORECASE),
    re.compile(r"\b(back(wards)?[- ]?compat(ibility)?|compat(ibility)?)[- ]shim\b", re.IGNORECASE),
]
# "the old workaround is gone": a comment recording that one was removed
_REMOVED = re.compile(r"\bwork[- ]?arounds?\b.{0,30}\b(is|was|are|were|got)\s+(gone|removed|deleted|dropped)\b|"
                      r"\bno (longer )?(need|needs|needed)\b.{0,20}\bwork[- ]?around", re.IGNORECASE)
_RACE_WORDS = re.compile(r"\b(race|racy|flak\w*|timing|in time|give \w+ (a moment|time)|let \w+ (finish|catch up)|"
                         r"eventually consistent|not ready yet|before it'?s ready|give (\w+ ){1,3}(a moment|time))\b", re.IGNORECASE)
_TIMER_CALL = re.compile(r"\b(time\.sleep|asyncio\.sleep|sleep|setTimeout|wait_for|retry|backoff)\s*\(", re.IGNORECASE)


def _py_comments(text: str) -> list[tuple[int, str]]:
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string.lstrip("#").strip()))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    return out


def _outside_quotes(line: str, marker: str) -> int:
    """Index of *marker* outside a string literal on this line, or -1."""
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif line.startswith(marker, i):
            return i
        i += 1
    return -1


def _c_comments(rest: str) -> tuple[list[str], bool]:
    """The comment texts on a C-family line (after any block comment it continues), and whether a
    ``/*`` on it is left open. A ``/*`` or ``//`` counts wherever it starts, outside quotes."""
    texts: list[str] = []
    while True:
        b, ln = _outside_quotes(rest, "/*"), _outside_quotes(rest, "//")
        if ln >= 0 and (b < 0 or ln < b):
            texts.append(rest[ln + 2:].strip())
            return texts, False
        if b < 0:
            return texts, False
        end = rest.find("*/", b + 2)
        if end < 0:
            texts.append(rest[b + 2:].strip().lstrip("*").strip())
            return texts, True
        texts.append(rest[b + 2:end].strip().lstrip("*").strip())
        rest = rest[end + 2:]


def _line_comments(text: str, marker: str) -> list[tuple[int, str]]:
    out = []
    in_block = False
    for n, line in enumerate(text.splitlines(), start=1):
        if marker == "//":
            texts: list[str] = []
            rest = line
            if in_block:
                end = line.find("*/")
                texts.append((line if end < 0 else line[:end]).strip().lstrip("*").strip())
                rest = "" if end < 0 else line[end + 2:]
                in_block = end < 0
            if not in_block:
                more, in_block = _c_comments(rest)
                texts += more
            if any(texts):
                out.append((n, " ".join(t for t in texts if t)))
            continue
        i = _outside_quotes(line, marker)
        if i >= 0 and not (marker == "#" and line[i:i + 2] == "#!"):
            out.append((n, line[i + len(marker):].strip()))
    return out


def comments(path: str | Path, text: str) -> list[tuple[int, str]]:
    suffix = Path(path).suffix
    if suffix == ".py":
        return _py_comments(text)
    if suffix in (".sh", ".bash"):
        return _line_comments(text, "#")
    return _line_comments(text, "//")


def classify(comment: str, lines: list[str], line_no: int) -> str:
    """``marker``, ``race_timer`` or "" for one comment at *line_no* (1-based) of *lines*."""
    if ACCEPTED.search(comment) or _REMOVED.search(comment):
        return ""
    if any(p.search(comment) for p in _MARKERS):
        return "marker"
    if _RACE_WORDS.search(comment):
        near = lines[line_no - 1:line_no + 2]
        if any(_TIMER_CALL.search(x) for x in near):
            return "race_timer"
    return ""


def _scanned(rel: str | Path) -> bool:
    """Whether a repo-relative file is one the scan reads: a source suffix, not minified or a type
    declaration, and under no skipped or dot directory. Full walks and explicit paths agree."""
    p = Path(rel)
    return (p.suffix in SUFFIXES and not p.name.endswith((".min.js", ".d.ts"))
            and not any(d in SKIP_DIRS or d.startswith(".") for d in p.parts[:-1]))


def _files(root: Path, rel_paths: list[str] | None) -> list[Path]:
    if rel_paths is not None:
        return [root / p for p in rel_paths if _scanned(p)]
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        out.extend(Path(dirpath) / f for f in filenames if _scanned(f))
    return sorted(out)


def scan(repo_name: str, repo_path: str | Path, rel_paths: list[str] | None = None) -> list[Finding]:
    """Findings for every workaround comment under *repo_path* (or just *rel_paths*). Reads only."""
    root = Path(repo_path)
    findings: list[Finding] = []
    for path in _files(root, rel_paths):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # gone or unreadable: nothing to flag in it
        lines = text.splitlines()
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        for line_no, comment in comments(path, text):
            kind = classify(comment, lines, line_no)
            if not kind:
                continue
            short = re.sub(r"\s+", " ", comment)[:80]
            findings.append(Finding(
                id=f"{SOURCE}:{kind}:{rel}:{line_no}",
                type="maintainability",
                severity="MEDIUM" if kind == "race_timer" else "LOW",
                repo=repo_name,
                file_path=str(path),
                line=line_no,
                title=("Timer or retry papering over a race: " if kind == "race_timer" else "Workaround comment: ")
                      + short,
                description=("A comment justifies a sleep, delay or retry by a race or timing problem. The race is "
                             "still there; the timer only makes it rarer, and agents copy the pattern."
                             if kind == "race_timer" else
                             "A comment says the code around it works around a problem. Agents copy what is in "
                             "the repo, so the workaround spreads unless the cause is fixed."),
                evidence="\n".join(f"{n}| {lines[n - 1]}" for n in range(max(1, line_no - 1),
                                                                         min(len(lines), line_no + 2) + 1)),
                recommendation=("Fix the cause the comment names, then delete the workaround and the comment. If it "
                                "must stay, say why it is permanent and mark the line `workaround: ok`."),
                source=SOURCE,
            ))
    return findings
