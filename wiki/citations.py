"""Mechanical citation checks on wiki pages, and dropping claims whose citation fails.

A citation is ``[path:Lstart-Lend]`` with a repo-relative path. It passes when the file exists,
the range is in bounds and, if a backticked identifier precedes it in the same sentence, that
identifier occurs in the cited lines. Claims with a failing citation are removed (#8: "drop
claims whose file:line doesn't exist, is out of bounds, or doesn't contain the symbol the
sentence names"), never rewritten by this pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wiki.sources import content_hash, read_text

CITE = re.compile(r"\[([\w./@+-]+?\.\w+):L(\d+)(?:-L?(\d+))?\]")
_NAMED = re.compile(r"`([A-Za-z_][\w.]*)(?:\(\))?`")
_FILE_EXT = {"py", "ts", "tsx", "js", "jsx", "md", "json", "yaml", "yml", "toml", "sh", "txt", "cfg", "ini"}
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass
class CitationReport:
    total: int = 0
    valid: int = 0
    named: int = 0
    matched: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"total": self.total, "valid": self.valid, "named": self.named, "matched": self.matched,
                "failed": self.failed[:20]}


def _confined(root: Path, path: str) -> Path | None:
    """The cited file under ``root``, or None if it is missing or the path leaves the root.

    Citation paths come from LLM-written pages, so an absolute path or ``..`` must never make
    us read (or hash) a file outside the repo.
    """
    if Path(path).is_absolute() or ".." in Path(path).parts:
        return None
    f = (root / path).resolve()
    return f if f.is_file() and root.resolve() in f.parents else None


class _Files:
    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[str, list[str] | None] = {}

    def lines(self, path: str) -> list[str] | None:
        if path not in self._cache:
            f = _confined(self.root, path)
            text = read_text(f) if f is not None else None
            self._cache[path] = text.splitlines() if text is not None else None
        return self._cache[path]


def _verdict(md: str, m: re.Match, files: _Files) -> tuple[str, str | None]:
    """("ok" | "missing" | "range" | "symbol", named identifier or None)."""
    path, a = m.group(1), int(m.group(2))
    b = int(m.group(3) or a)
    lines = files.lines(path)
    if lines is None:
        return "missing", None
    if not (1 <= a <= b <= len(lines)):
        return "range", None
    line_start = md.rfind("\n", 0, m.start()) + 1
    prev = max((c.end() for c in CITE.finditer(md, line_start, m.start())), default=line_start)
    names = [x for x in _NAMED.findall(md, prev, m.start()) if x.rsplit(".", 1)[-1] not in _FILE_EXT]
    if not names:
        return "ok", None
    # The clause passes if ANY identifier it names occurs in the cited lines: table rows and
    # lists name several things ("`CheckFailed` (`NudeNetCheckFailed`, `GuardCheckFailed`)")
    # and only one of them need be at the cited lines. A module path naming the cited file
    # itself (`pkg.xmp` citing xmp.py) also counts. Wrong-line citations still fail.
    cited = "\n".join(lines[a - 1:b])
    stem = Path(path).stem
    for name in reversed(names):
        ident = name.split(".")[-1]
        if ident == stem or re.search(rf"\b{re.escape(ident)}\b", cited):
            return "ok", name
    return "symbol", names[-1]


def check(md: str, root: Path) -> CitationReport:
    files, rep = _Files(root), CitationReport()
    for m in CITE.finditer(md):
        rep.total += 1
        v, name = _verdict(md, m, files)
        if v in ("ok", "symbol"):
            rep.valid += 1
        if name:
            rep.named += 1
            rep.matched += v == "ok"
        if v != "ok":
            rep.failed.append({"citation": m.group(0), "reason": v, **({"symbol": name} if name else {})})
    return rep


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*\[(])")
_BLOCK_PREFIX = re.compile(r"^\s*(?:>\s*)*")


def _whole_unit(line: str) -> bool:
    """A list item, table row or heading: dropped whole when one of its citations fails."""
    stripped = line.lstrip()
    return stripped.startswith(("- ", "* ", "+ ", "|", "#")) or bool(re.match(r"\d+[.)] ", stripped))


def _starts_block(line: str) -> bool:
    """A line that can never continue the block above it (hard-wrapped text continues it)."""
    return _whole_unit(line) or bool(_FENCE.match(line))


def drop_failed_claims(md: str, root: Path) -> tuple[str, list[dict[str, str]]]:
    """Remove every claim whose citation fails. Returns (page, dropped claims).

    Lines are grouped into logical blocks first, so hard-wrapped text is judged whole: a
    paragraph is a run of non-blank lines, a list item takes its continuation lines, and table
    rows and headings are one line each. List items, table rows and headings with a failing
    citation are removed whole; in a paragraph only the sentence holding it goes (the paragraph is
    then re-joined onto one line). Code fences are left alone.
    """
    files = _Files(root)
    out: list[str] = []
    dropped: list[dict[str, str]] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):  # a whole fence, verbatim (blank lines inside it included)
            out.append(line)
            i += 1
            while i < len(lines):
                out.append(lines[i])
                i += 1
                if _FENCE.match(lines[i - 1]):
                    break
            continue
        if not line.strip():
            if not (out and not out[-1].strip()):  # collapse blank runs (e.g. left by a dropped claim)
                out.append(line)
            i += 1
            continue
        block = [line]
        i += 1
        if not line.lstrip().startswith(("|", "#")):  # rows and headings are single lines
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                block.append(lines[i])
                i += 1
        text = block[0] if len(block) == 1 else \
            " ".join([block[0].rstrip()] + [_BLOCK_PREFIX.sub("", ln).strip() for ln in block[1:]])
        bad = [(m, v) for m in CITE.finditer(text) if (v := _verdict(text, m, files)[0]) != "ok"]
        if not bad:
            out.extend(block)
            continue
        if _whole_unit(block[0]):
            dropped.append({"claim": " ".join(text.split()), "reason": bad[0][1], "citation": bad[0][0].group(0)})
            continue
        # sentence spans, so a failing citation removes only the sentence it sits in (by position:
        # two sentences can cite the same range and only one of them name the wrong symbol)
        # The first line's indentation / blockquote markers stay whatever sentences are dropped.
        prefix = _BLOCK_PREFIX.match(text).group(0)
        bounds = [len(prefix)] + [s.end() for s in _SENTENCE_END.finditer(text, len(prefix))] + [len(text)]
        kept = []
        for start, end in zip(bounds, bounds[1:]):
            sentence = text[start:end]
            hits = [(m, v) for m, v in bad if start <= m.start() < end]
            if hits:
                dropped.append({"claim": sentence.strip(), "reason": hits[0][1], "citation": hits[0][0].group(0)})
            else:
                kept.append(sentence)
        if kept and "".join(kept).strip():
            out.append(prefix + " ".join(s.strip() for s in kept))
    return "\n".join(out).strip() + "\n", dropped


def cited_files(md: str, root: Path) -> dict[str, str]:
    """Repo-relative path → content hash for every cited file that exists (for rebuild checks)."""
    out: dict[str, str] = {}
    for path in sorted({m.group(1) for m in CITE.finditer(md)}):
        f = _confined(root, path)
        if f is not None and (text := read_text(f)) is not None:
            out[path] = content_hash(text)
    return out


def _ranges(md: str) -> dict[str, list[list[int]]]:
    out: dict[str, set[tuple[int, int]]] = {}
    for m in CITE.finditer(md):
        a = int(m.group(2))
        out.setdefault(m.group(1), set()).add((a, int(m.group(3) or a)))
    return {path: [list(r) for r in sorted(rs)] for path, rs in sorted(out.items())}


def range_hash(lines: list[str], ranges: list[list[int]]) -> str | None:
    """Hash of exactly the cited lines; None if a range no longer exists in the file."""
    if any(not (1 <= a <= b <= len(lines)) for a, b in ranges):
        return None
    return content_hash("\x00".join("\n".join(lines[a - 1:b]) for a, b in ranges))


def cited_ranges(md: str, root: Path) -> dict[str, dict[str, object]]:
    """Path → {ranges, hash of the text at those ranges} for every cited file that exists.

    A page only needs rebuilding when the code it actually cites changed. Lines shifting
    under a citation also changes the text at those ranges, so it counts as a change: the
    page's line numbers would be wrong otherwise.
    """
    out: dict[str, dict[str, object]] = {}
    for path, ranges in _ranges(md).items():
        f = _confined(root, path)
        if f is not None and (text := read_text(f)) is not None:
            h = range_hash(text.splitlines(), ranges)
            if h is not None:
                out[path] = {"ranges": ranges, "hash": h}
    return out


def cited_code(md: str, root: Path, pad: int = 3, cap_chars: int = 120_000) -> str:
    """The exact code every citation points at (line-numbered, padded, merged), for check-and-fix."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    for m in CITE.finditer(md):
        a = int(m.group(2))
        ranges.setdefault(m.group(1), []).append((a, int(m.group(3) or a)))
    files, parts, size = _Files(root), [], 0
    for path, rs in sorted(ranges.items()):
        lines = files.lines(path)
        if lines is None:
            parts.append(f"\n===== {path} (FILE DOES NOT EXIST) =====")
            continue
        merged: list[list[int]] = []
        for a, b in sorted(rs):
            if not (1 <= a <= b <= len(lines)):  # say so, rather than show an empty block
                parts.append(f"\n===== {path} L{a}-L{b} (LINES DO NOT EXIST; file has {len(lines)} lines) =====")
                continue
            a, b = max(1, a - pad), min(len(lines), b + pad)
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            chunk = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(a, b + 1))
            if size + len(chunk) > cap_chars:
                parts.append(f"\n===== {path} L{a}-L{b} (not shown: size cap) =====")
                continue
            size += len(chunk)
            parts.append(f"\n===== {path} L{a}-L{b} =====\n{chunk}")
    return "\n".join(parts)
