from __future__ import annotations

from pathlib import Path

import pytest

import copilot.context_orchestrator as co
from copilot.copilot_engine import CopilotEngine


def test_trim_keeps_the_tail_when_it_has_no_entry_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(co, "_NOTES_MAX_BYTES", 1000)
    notes = tmp_path / "notes.md"
    body = "".join(f"line {i} of one oversized entry\n" for i in range(100))
    notes.write_text("## 2026-01-01\n" + body, encoding="utf-8")
    co._trim_notes(notes)
    kept = notes.read_text(encoding="utf-8")
    assert kept and kept.endswith("line 99 of one oversized entry\n")
    assert len(kept.encode()) <= 500


def test_trim_starts_at_a_line_start_entry_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(co, "_NOTES_MAX_BYTES", 1000)
    notes = tmp_path / "notes.md"
    old = "## 2026-01-01\n" + "filler\n" * 120
    # the start of the kept tail holds an inline '## ' and a '### ' subheading before the next real entry
    newest = "see the ## marker inline\n### sub heading\n## 2026-02-02\n- query: newest\n" + "x\n" * 150
    notes.write_text(old + newest, encoding="utf-8")
    co._trim_notes(notes)
    kept = notes.read_text(encoding="utf-8")
    assert kept.startswith("## 2026-02-02\n- query: newest\n")


def test_extraction_citations_name_the_configured_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "data" / "reports"
    out.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_AUDIT_OUTPUT_DIR", str(out))
    engine = CopilotEngine(search=None, graph=None)

    missing = engine.ask("show the top extraction candidate", enable_notes=False)
    assert missing.citations == [str(out / "extraction_candidates.yaml")]

    (out / "extraction_candidates.yaml").write_text("candidates:\n  - title: Shared retry\n    repos_involved: [a]\n")
    found = engine.ask("show the top extraction candidate", enable_notes=False)
    assert found.citations == [str(out / "extraction_candidates.yaml"), str(out / "extraction_candidates.md")]
