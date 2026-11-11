"""Atomic LATEST.json / LATEST.md publish for mithrandir."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from conductor.schemas import MithrandirStatus, validate_status_dict

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\b[\w']+\b")


def public_base_url() -> str:
    return os.getenv("AI_AUDIT_PUBLIC_BASE_URL", "http://192.168.1.198:8765").rstrip("/")


def mithrandir_dir(output_dir: str | Path = "reports") -> Path:
    return Path(output_dir) / "mithrandir"


def latest_urls(base: str | None = None) -> tuple[str, str]:
    root = (base or public_base_url()).rstrip("/")
    return (
        f"{root}/reports/mithrandir/LATEST.json",
        f"{root}/reports/mithrandir/LATEST.md",
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(path, payload)


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def md_passes_tts_rules(md: str) -> tuple[bool, list[str]]:
    """Return (ok, reasons) for Holly TTS constraints."""
    reasons: list[str] = []
    words = count_words(md)
    if words > 150:
        reasons.append(f"word_count={words}>150")
    if "|" in md and re.search(r"\|.+\|", md):
        reasons.append("contains_table")
    if re.search(r"!\[|\[!\[|badge", md, re.I):
        reasons.append("badge_or_image")
    # Absolute paths or long relative paths (more than basename)
    for m in re.finditer(r"(?:/[A-Za-z0-9._-]+){2,}|[A-Za-z]:\\[^\s]+", md):
        reasons.append(f"long_path:{m.group(0)[:40]}")
        break
    if re.search(r"https?://\S+", md):
        reasons.append("raw_https_url")
    return (len(reasons) == 0, reasons)


@dataclass(slots=True)
class PublishResult:
    json_path: Path
    md_path: Path
    archive_json: Path | None
    archive_md: Path | None
    status: dict[str, Any]
    markdown: str


def publish_status(
    status: MithrandirStatus,
    markdown: str,
    output_dir: str | Path = "reports",
    *,
    archive: bool = True,
) -> PublishResult:
    """Atomically publish LATEST.json (envelope) and LATEST.md; optionally archive."""
    # body_md is the envelope TTS/wall field; LATEST.md mirrors it for HTTP GET.
    status.body_md = markdown.rstrip() + "\n"
    speakable_ok, reasons = md_passes_tts_rules(markdown)
    if not speakable_ok:
        status.speakable = False
        logger.warning("LATEST.md / body_md failed TTS rules: %s", reasons)
    else:
        status.speakable = bool(status.speakable) and speakable_ok

    data = status.to_dict()
    errors = validate_status_dict(data)
    if errors:
        raise ValueError("invalid mithrandir status: " + "; ".join(errors))

    root = mithrandir_dir(output_dir)
    json_path = root / "LATEST.json"
    md_path = root / "LATEST.md"
    _atomic_write_json(json_path, data)
    _atomic_write_text(md_path, markdown.rstrip() + "\n")

    archive_json = archive_md = None
    if archive:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
        arch = root / "archive"
        archive_json = arch / f"{stamp}.json"
        archive_md = arch / f"{stamp}.md"
        _atomic_write_json(archive_json, data)
        _atomic_write_text(archive_md, markdown.rstrip() + "\n")

    return PublishResult(
        json_path=json_path,
        md_path=md_path,
        archive_json=archive_json,
        archive_md=archive_md,
        status=data,
        markdown=markdown,
    )


def load_latest(output_dir: str | Path = "reports") -> dict[str, Any] | None:
    path = mithrandir_dir(output_dir) / "LATEST.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
