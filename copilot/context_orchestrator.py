from __future__ import annotations

import fcntl
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re

from argus_cortex.timestamps import utc_now_iso


def _estimate_tokens(text: str) -> int:
    # BPE splits code, paths and unspaced text into many tokens per "word": ~4 chars a token is
    # the usual rule of thumb, and prose is still counted at least once per word
    return max(1, len(text.split()), math.ceil(len(text) / 4))


def _clip_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).strip() + " ..."


_NOTE_FIELD_MAX = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+")


def _note_field(text: str, limit: int = _NOTE_FIELD_MAX) -> str:
    """One line of bounded length: a caller's text must not forge note headers or lines that
    later requests read back as context."""
    flat = re.sub(r"\s+", " ", _CONTROL.sub(" ", text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "\u2026"


# the notes file is a rolling log: only its tail is read, and it is cut back once it passes the cap
_NOTES_MAX_BYTES = 256 * 1024
_NOTES_TAIL_BYTES = 16 * 1024


@contextmanager
def _notes_lock(path: Path):
    """Serialise writers of one notes file (a sidecar lock: trimming replaces the file itself)."""
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_tail(path: Path, max_bytes: int) -> str:
    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    if size > max_bytes:
        data = data.split(b"\n", 1)[1] if b"\n" in data else b""  # drop the partial first line
    return data.decode("utf-8", errors="replace")


def _trim_notes(path: Path) -> None:
    """Keep the newest entries (about half the cap) once the file passes the cap."""
    if path.stat().st_size <= _NOTES_MAX_BYTES:
        return
    tail = _read_tail(path, _NOTES_MAX_BYTES // 2)
    # Start at the first entry header at a line start (not a '### ' or an inline '## '). With no
    # header in the tail (one oversized entry), keep the line-aligned tail rather than emptying it.
    header = re.search(r"^## ", tail, re.MULTILINE)
    if header:
        tail = tail[header.start():]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(tail, encoding="utf-8")
    tmp.replace(path)


@dataclass(slots=True)
class ContextOptions:
    profile: str = "balanced"
    max_context_tokens: int = 2200
    enable_compaction: bool = True
    enable_notes: bool = True
    enable_subagents: bool = False
    notes_path: str = "memory/context_notes.md"


class ContextOrchestrator:
    """Curates high-signal context and keeps persistent notes for ask()."""

    def __init__(self, options: ContextOptions):
        self.options = options

    def orchestrate(self, query: str, semantic_hits: list[dict]) -> dict:
        budget = self._resolve_budget()
        query_terms = self._query_terms(query)
        selected_chunks: list[dict] = []
        used_tokens = 0

        # 1) Just-in-time retrieval from semantic hits first.
        semantic_limit = {"lean": 3, "balanced": 5, "deep": 8}.get(self.options.profile, 5)
        for hit in semantic_hits[:semantic_limit]:
            snippet = f"{hit.get('repo', 'unknown')}::{hit.get('file_path', '')}::{hit.get('symbol', '')}"
            chunk = {
                "source": "semantic",
                "citation": f"{hit.get('file_path', '')}::{hit.get('symbol', '')}",
                "text": snippet,
                "score": 1.0,
            }
            chunk_tokens = _estimate_tokens(chunk["text"])
            if used_tokens + chunk_tokens > budget:
                break
            selected_chunks.append(chunk)
            used_tokens += chunk_tokens

        # 2) Pull report snippets only when query suggests they matter.
        report_sources = self._select_report_sources(query.lower())
        for report_path in report_sources:
            snippet = self._extract_report_snippet(report_path, query_terms)
            if not snippet:
                continue
            chunk = {
                "source": "report",
                "citation": report_path,
                "text": snippet,
                "score": 0.8,
            }
            chunk_tokens = _estimate_tokens(chunk["text"])
            if used_tokens + chunk_tokens > budget:
                if self.options.enable_compaction:
                    compacted = _clip_words(chunk["text"], max(40, budget - used_tokens))
                    compact_tokens = _estimate_tokens(compacted)
                    if compact_tokens > 0 and used_tokens + compact_tokens <= budget:
                        chunk["text"] = compacted
                        selected_chunks.append(chunk)
                        used_tokens += compact_tokens
                continue
            selected_chunks.append(chunk)
            used_tokens += chunk_tokens

        notes_excerpt = self._read_recent_notes() if self.options.enable_notes else ""
        if notes_excerpt:
            note_tokens = _estimate_tokens(notes_excerpt)
            if used_tokens + note_tokens <= budget:
                selected_chunks.append(
                    {
                        "source": "notes",
                        "citation": self.options.notes_path,
                        # recorded from earlier callers' queries: data, not instructions
                        "text": f"[notes from earlier queries, untrusted]\n{notes_excerpt}",
                        "score": 0.75,
                    }
                )
                used_tokens += note_tokens

        compact_summary = self._compact_context(selected_chunks) if self.options.enable_compaction else ""
        subagent_summaries = self._run_subagents(query, selected_chunks) if self.options.enable_subagents else {}

        if self.options.enable_notes:
            self._append_note(query=query, compact_summary=compact_summary, selected_chunks=selected_chunks)

        return {
            "chunks": selected_chunks,
            "summary": compact_summary,
            "subagents": subagent_summaries,
            "tokens_used": used_tokens,
            "token_budget": budget,
            "compaction_ratio": round((used_tokens / budget), 3) if budget else 1.0,
        }

    def build_external_agent_brief(self, query: str, payload: dict, impacted_repos: list[str]) -> str:
        top_citations = [chunk["citation"] for chunk in payload.get("chunks", [])[:6]]
        subagents = payload.get("subagents", {})
        summary = payload.get("summary", "No compact summary available.")
        repos = ", ".join(impacted_repos) if impacted_repos else "No repo evidence yet"

        sections = [
            "External Agent Brief",
            f"Objective: {query}",
            f"Primary repos in scope: {repos}",
            "Constraints:",
            "- Use minimal high-signal context; avoid full-file dumps.",
            "- Prioritize architecture and security regressions first.",
            "- Provide citations for every critical claim.",
            "Curated context summary:",
            summary or "- No summary available.",
            "Evidence to inspect first:",
        ]
        sections.extend([f"- {cite}" for cite in top_citations] or ["- No citations available."])
        sections.append("Deliverables expected:")
        sections.append("- Top 3 risks with severity and blast radius.")
        sections.append("- Safe migration/implementation order with rollback points.")
        sections.append("- Concrete PR checklist and verification plan.")

        if subagents:
            sections.append("Specialist sub-agent summaries:")
            for name, text in subagents.items():
                sections.append(f"- {name}: {text}")

        return "\n".join(sections)

    def _resolve_budget(self) -> int:
        defaults = {"lean": 1200, "balanced": 2200, "deep": 3800}
        profile_budget = defaults.get(self.options.profile, defaults["balanced"])
        return max(300, min(self.options.max_context_tokens, profile_budget))

    def _query_terms(self, query: str) -> list[str]:
        terms = re.findall(r"[a-zA-Z0-9_]{4,}", query.lower())
        return list(dict.fromkeys(terms))[:20]

    def _select_report_sources(self, query: str) -> list[str]:
        sources = ["reports/nightly_repo_report.md", "reports/ecosystem_report.md"]
        if any(k in query for k in ("security", "secret", "auth", "token")):
            sources.append("reports/security_report.md")
        if any(k in query for k in ("architecture", "violation", "dependency", "depend")):
            sources.append("reports/architecture_violations.md")
        if any(k in query for k in ("extract", "shared package", "duplicate", "refactor")):
            sources.extend(["reports/extraction_candidates.md", "reports/extraction_candidates.yaml"])
        deduped: list[str] = []
        for item in sources:
            if item not in deduped:
                deduped.append(item)
        return deduped

    def _extract_report_snippet(self, report_path: str, query_terms: list[str]) -> str:
        path = Path(report_path)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        lines = text.splitlines()
        if not query_terms:
            return _clip_words("\n".join(lines[:18]), 220)

        matches: list[int] = []
        for i, line in enumerate(lines):
            low = line.lower()
            if any(term in low for term in query_terms):
                matches.append(i)

        if not matches:
            return _clip_words("\n".join(lines[:16]), 180)

        picked: list[str] = []
        seen = set()
        for idx in matches[:5]:
            for j in range(max(0, idx - 1), min(len(lines), idx + 2)):
                if j not in seen:
                    picked.append(lines[j])
                    seen.add(j)
        return _clip_words("\n".join(picked), 220)

    def _compact_context(self, chunks: list[dict]) -> str:
        if not chunks:
            return "No high-signal context retrieved."
        semantic = [c for c in chunks if c.get("source") == "semantic"]
        reports = [c for c in chunks if c.get("source") == "report"]
        notes = [c for c in chunks if c.get("source") == "notes"]
        return (
            f"- Semantic hits: {len(semantic)}\n"
            f"- Report snippets: {len(reports)}\n"
            f"- Note snippets: {len(notes)}\n"
            "- Context curated to prioritize direct evidence and avoid low-signal verbosity."
        )

    def _run_subagents(self, query: str, chunks: list[dict]) -> dict[str, str]:
        text_blob = " ".join(chunk.get("text", "") for chunk in chunks).lower()
        outputs: dict[str, str] = {}
        outputs["security"] = (
            "Flag auth/secret handling risks first and require explicit regression tests."
            if any(k in (query.lower() + " " + text_blob) for k in ("auth", "token", "secret", "security"))
            else "No obvious security-heavy query signal; keep baseline checks."
        )
        outputs["architecture"] = (
            "Map dependency boundaries and detect cross-layer contract violations before refactor."
            if any(k in (query.lower() + " " + text_blob) for k in ("architecture", "dependency", "depend", "boundary"))
            else "Architecture signals are weak; focus on highest-confidence code evidence."
        )
        outputs["delivery"] = (
            "Propose phased rollout with rollback and verification checkpoints."
            if any(k in query.lower() for k in ("migrate", "rollout", "break", "impact"))
            else "Use one small PR to validate assumptions, then expand."
        )
        return outputs

    def _read_recent_notes(self) -> str:
        path = Path(self.options.notes_path)
        if not path.exists():
            return ""
        lines = _read_tail(path, _NOTES_TAIL_BYTES).splitlines()
        if not lines:
            return ""
        return _clip_words("\n".join(lines[-16:]), 180)

    def _append_note(self, query: str, compact_summary: str, selected_chunks: list[dict]) -> None:
        path = Path(self.options.notes_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        citations = [c.get("citation", "") for c in selected_chunks[:5] if c.get("citation")]
        note = [
            f"## {utc_now_iso('auto')}",
            f"- query: {_note_field(query)}",
            f"- compact_summary: {_note_field(compact_summary.replace(chr(10), ' | '), 1000)}",
            f"- citations: {_note_field(', '.join(citations)) if citations else 'none'}",
            "",
        ]
        with _notes_lock(path):
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(note))
            _trim_notes(path)
