"""Per-file notes (Haiku): what a file does, its key symbols with line ranges, its surface.

Readers never see notes; pages are drafted from them. Notes are cached by a hash of the file's
path and content, so a rebuild only pays for files that changed. The cache key is the one the
2026-11-13 eval used, and its cache (``reports/wiki_eval/notes``) is read as a fallback, so the
notes it already wrote for the whole ecosystem are reused rather than paid for again.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auditor.repo_resolver import resolve_scan_root
from wiki.sources import MAX_FILE_CHARS, numbered, read_text, rel, source_files

NOTES_MODEL = "claude-haiku-5-5"
NOTES_EFFORT = "low"
NOTES_MAX_TOKENS = 8000

NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "role": {"type": "string"},
        "key_symbols": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "purpose": {"type": "string"},
                },
                "required": ["name", "kind", "start_line", "end_line", "purpose"],
                "additionalProperties": False,
            },
        },
        "interfaces": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "role", "key_symbols", "interfaces", "dependencies"],
    "additionalProperties": False,
}

NOTE_PROMPT = """You are indexing a codebase to build a developer wiki. Read the file below \
(line numbers are prefixed) and return a compact note:
- summary: 2-4 sentences on what the file does.
- role: one of entrypoint, api, service, model, config, util, test, script, ui, other — plus a few words.
- key_symbols: the functions/classes/routes that matter to someone understanding the repo, with \
exact start_line/end_line from the prefixes. Skip trivial helpers. At most 12.
- interfaces: externally visible surface defined here (HTTP routes with method+path, CLI commands, \
env vars read, queues/topics, DB tables).
- dependencies: other ecosystem repos, external services or notable libraries this file talks to.
Be factual; only state what the code shows.

Repository: {repo}
File: {path}

{body}"""


def notes_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / "wiki" / "_notes"


def legacy_notes_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / "wiki_eval" / "notes"


def note_key(relpath: str, text: str) -> str:
    return hashlib.sha1(f"{relpath}\0{text}".encode()).hexdigest()[:16]


def _find_note(output_dir: str | Path, repo: str, key: str) -> Path | None:
    for base in (notes_dir(output_dir), legacy_notes_dir(output_dir)):
        p = base / repo / f"{key}.json"
        if p.is_file():
            return p
    return None


@dataclass
class NoteJob:
    repo: str
    path: str
    text: str
    key: str

    @property
    def custom_id(self) -> str:
        # batch custom_ids allow [a-zA-Z0-9_-]{1,64}
        return f"n-{self.key}-{hashlib.sha1(self.repo.encode()).hexdigest()[:8]}"


def scan_notes(repo, output_dir: str | Path) -> tuple[list[NoteJob], int, list[str]]:
    """(files still needing a note, files already noted, oversize files skipped)."""
    root = resolve_scan_root(repo)
    jobs, cached, oversize = [], 0, []
    for f in source_files(repo):
        text = read_text(f)
        if text is None or not text.strip():
            continue
        rp = rel(f, root)
        if len(text) > MAX_FILE_CHARS:
            oversize.append(f"{rp} ({len(text) // 1000}k chars)")
            continue
        key = note_key(rp, text)
        if _find_note(output_dir, repo.name, key):
            cached += 1
        else:
            jobs.append(NoteJob(repo.name, rp, text, key))
    return jobs, cached, oversize


def note_params(job: NoteJob) -> dict[str, Any]:
    return {
        "model": NOTES_MODEL,
        "max_tokens": NOTES_MAX_TOKENS,
        "output_config": {"effort": NOTES_EFFORT, "format": {"type": "json_schema", "schema": NOTE_SCHEMA}},
        "messages": [{"role": "user", "content": NOTE_PROMPT.format(repo=job.repo, path=job.path,
                                                                     body=numbered(job.text))}],
    }


def save_note(job: NoteJob, text: str, output_dir: str | Path) -> bool:
    """Write the note from a model reply; False if the reply isn't a valid note."""
    try:
        note = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(note, dict) or not all(k in note for k in NOTE_SCHEMA["required"]):
        return False
    note.update(path=job.path, lines=job.text.count("\n") + 1)
    out = notes_dir(output_dir) / job.repo / f"{job.key}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(note, indent=1), encoding="utf-8")
    return True


def load_notes(repo, output_dir: str | Path) -> list[dict[str, Any]]:
    """Notes for the repo's current files (files without a note are left out)."""
    root = resolve_scan_root(repo)
    notes = []
    for f in source_files(repo):
        text = read_text(f)
        if text is None or len(text) > MAX_FILE_CHARS:
            continue
        p = _find_note(output_dir, repo.name, note_key(rel(f, root), text))
        if p:
            notes.append(json.loads(p.read_text(encoding="utf-8")))
    return notes
