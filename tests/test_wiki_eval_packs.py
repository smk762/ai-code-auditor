"""Regression tests for audit-pack fixes in scripts/wiki_eval.py (no model calls: `call` is faked)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def we(monkeypatch):
    spec = importlib.util.spec_from_file_location("wiki_eval", ROOT / "scripts" / "wiki_eval.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "client", lambda: object())
    return m


@pytest.fixture
def repo(tmp_path: Path, monkeypatch, we):
    root = tmp_path / "svc"
    root.mkdir()
    (root / "app.py").write_text("def f():\n    return 1\n")
    (tmp_path / "secret.txt").write_text("TOP SECRET\n")
    r = SimpleNamespace(name="svc", path=str(root), subpath="")
    monkeypatch.setattr(we, "resolve_scan_root", lambda repo: root)
    monkeypatch.setattr(we, "repos_by_name", lambda: {"svc": r})
    return r


def test_cited_code_never_reads_outside_the_repo(we, repo, tmp_path: Path) -> None:
    md = f"[app.py:L1-L2] [../secret.txt:L1] [{tmp_path}/secret.txt:L1]"
    shown = we.cited_code(md, repo, pad=0)
    assert "TOP SECRET" not in shown
    assert "app.py L1-L2" in shown and shown.count("FILE DOES NOT EXIST") == 2


def test_one_failed_page_keeps_every_other_record(we, repo, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(we, "load_notes", lambda repo: [{"path": "app.py"}])
    monkeypatch.setattr(we, "repo_context", lambda repo, notes: "ctx")
    monkeypatch.setattr(we, "structure_block", lambda repo: "struct")
    monkeypatch.setattr(we, "check_citations", lambda text, repo: {"valid": 0, "total": 0, "matched": 0, "named": 0})

    def call(cl, model, effort, *, system=None, content, max_tokens, schema=None):
        if "**interfaces**" in content:
            raise RuntimeError("boom")
        return {"text": "page text", "latency_s": 1.0, "cost": 0.5}

    monkeypatch.setattr(we, "call", call)
    run = tmp_path / "run"
    results = we.stage_pages(["svc"], ["haiku"], run, concurrency=2)
    recs = [json.loads(line) for line in (run / "pages.jsonl").read_text().splitlines() if line.strip()]
    assert len(results) == len(recs) == len(we.PAGES)
    failed = [r for r in recs if "error" in r]
    assert [(r["page"], r["cost"]) for r in failed] == [("interfaces", 0.0)] and "boom" in failed[0]["error"]


def test_one_failed_judgement_keeps_the_others(we, repo, tmp_path: Path, monkeypatch) -> None:
    run = tmp_path / "run"
    for page in we.PAGES:
        d = run / "pages" / "svc" / "haiku"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{page}.md").write_text(f"# {page}\n")
    monkeypatch.setattr(we, "full_source", lambda repo: "src")

    def call(cl, model, effort, *, system=None, content, max_tokens, schema=None):
        if "# architecture" in content:
            raise RuntimeError("judge exploded")
        return {"text": json.dumps({"pages": []}), "latency_s": 1.0, "cost": 1.0}

    monkeypatch.setattr(we, "call", call)
    results = we.stage_judge(run, concurrency=2)
    recs = [json.loads(line) for line in (run / "judgements.jsonl").read_text().splitlines() if line.strip()]
    assert len(results) == len(recs) == len(we.PAGES)
    assert [r["page"] for r in recs if "error" in r] == ["architecture"]
    assert sum(r["cost"] for r in recs) == pytest.approx(len(we.PAGES) - 1)
