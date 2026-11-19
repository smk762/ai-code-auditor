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
    for arm in ("haiku", "opus"):  # judging compares arms: a page needs two
        for page in we.PAGES:
            d = run / "pages" / "svc" / arm
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


def test_pages_only_one_arm_wrote_are_not_judged(we, repo, tmp_path: Path, monkeypatch) -> None:
    run = tmp_path / "run"
    for page in we.PAGES:
        (run / "pages" / "svc" / "haiku").mkdir(parents=True, exist_ok=True)
        (run / "pages" / "svc" / "haiku" / f"{page}.md").write_text(f"# {page}\n")
    (run / "pages" / "svc" / "opus").mkdir(parents=True)
    (run / "pages" / "svc" / "opus" / "overview.md").write_text("# overview\n")
    monkeypatch.setattr(we, "full_source", lambda repo: "src")
    judged = []

    def call(cl, model, effort, *, system=None, content, max_tokens, schema=None):
        judged.append(content)
        return {"text": json.dumps({"pages": [], "ranking": []}), "latency_s": 1.0, "cost": 1.0}

    monkeypatch.setattr(we, "call", call)
    results = we.stage_judge(run, concurrency=2)
    assert [r["page"] for r in results] == ["overview"] and len(judged) == 1


def test_citations_past_eof_or_reversed_are_flagged_not_shown_empty(we, repo) -> None:
    shown = we.cited_code("[app.py:L50] [app.py:L2-L1] [app.py:L1]", repo, pad=3)
    assert shown.count("LINES OUT OF RANGE: file has 2 lines") == 2
    assert "app.py L1-L2 =====\n    1| def f():\n    2|     return 1" in shown
    assert "L47-L2" not in shown and "L50-L50 =====\n" not in shown.replace("RANGE", "")


def test_a_citation_one_line_past_eof_is_invalid(we, repo) -> None:
    c = we.check_citations("`f` [app.py:L3] and `f` [app.py:L1-L2]", repo)
    assert (c["total"], c["valid"]) == (2, 1)
    assert c["invalid_examples"] == ["[app.py:L3]"]


@pytest.mark.parametrize("src, exports", [
    ("__all__: list[str] = ['a', 'b']\n", ["a", "b"]),
    ("__all__ = ['a']\n__all__ += ['b']\n", ["a", "b"]),
    ("__all__ = ['a']\n__all__ += helpers.__all__\n", ["a", "<dynamic __all__>"]),
    ("__all__ = 5\n", ["<dynamic __all__>"]),
])
def test_annotated_augmented_and_odd_all_are_read(we, src: str, exports: list) -> None:
    assert we.file_structure(Path("m.py"), src)["exports"] == exports


@pytest.mark.parametrize("exc", [ValueError("source code string cannot contain null bytes"),
                                 RecursionError("maximum recursion depth exceeded")])
def test_unparsable_python_returns_empty_structure(we, monkeypatch, exc) -> None:
    import ast

    def parse(*a, **k):
        raise exc

    monkeypatch.setattr(ast, "parse", parse)
    assert we.file_structure(Path("m.py"), "x = 1\n") == {"imports": [], "exports": None, "top_level": []}


def _notes_env(we, repo, tmp_path: Path, monkeypatch, call) -> Path:
    root = Path(repo.path)
    (root / "b.py").write_text("def g():\n    return 2\n")
    (root / "c.py").write_text("def h():\n    return 3\n")
    notes = tmp_path / "notes"
    monkeypatch.setattr(we, "NOTES_DIR", notes)
    monkeypatch.setattr(we, "source_files", lambda repo: sorted(root.glob("*.py")))
    monkeypatch.setattr(we, "call", call)
    return notes


def _note_reply(text: str) -> dict:
    return {"text": text, "latency_s": 1.0, "cost": 0.25, "input_tokens": 10, "output_tokens": 5}


def test_one_bad_note_is_counted_not_fatal(we, repo, tmp_path: Path, monkeypatch) -> None:
    def call(cl, model, effort, *, system=None, content, max_tokens, schema=None):
        if "app.py" in content:
            return _note_reply("[1, 2]")  # valid JSON, not an object
        if "b.py" in content:
            raise RuntimeError("ledger unavailable")
        return _note_reply(json.dumps({"summary": "s", "role": "r", "key_symbols": [],
                                       "interfaces": [], "dependencies": []}))

    notes = _notes_env(we, repo, tmp_path, monkeypatch, call)
    stats = we.stage_notes(["svc"], concurrency=2)
    assert stats["errors"] == 2 and stats["cost"] == pytest.approx(0.5)
    assert len(list((notes / "svc").glob("*.json"))) == 1


def test_an_interrupted_note_write_leaves_no_cached_file(we, repo, tmp_path: Path, monkeypatch) -> None:
    good = json.dumps({"summary": "s", "role": "r", "key_symbols": [], "interfaces": [], "dependencies": []})
    notes = _notes_env(we, repo, tmp_path, monkeypatch, lambda *a, **k: _note_reply(good))
    real_write = Path.write_text

    def half_write(self, text, *a, **k):
        if self.suffix in (".json", ".tmp") and "notes" in self.parts:
            real_write(self, text[: len(text) // 2])
            raise OSError(28, "No space left on device")
        return real_write(self, text, *a, **k)

    monkeypatch.setattr(Path, "write_text", half_write)
    stats = we.stage_notes(["svc"], concurrency=1)
    assert stats["errors"] == 3
    assert not (notes / "svc").exists() or list((notes / "svc").iterdir()) == []


def _summary_run(tmp_path: Path, judgements: list[dict]) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    page = {"repo": "svc", "page": "overview", "cost": 1.0, "latency_s": 1.0, "words": 10,
            "citations": {"total": 1, "valid": 1, "anchored": 1, "named": 0, "matched": 0}}
    (run / "pages.jsonl").write_text("\n".join(json.dumps({**page, "arm": a}) for a in ("haiku", "opus")))
    (run / "judgements.jsonl").write_text("\n".join(json.dumps(j) for j in judgements))
    return run


def _score(label: str) -> dict:
    return {"label": label, "accuracy": 4, "coverage": 4, "usefulness": 4, "citation_quality": 4,
            "factual_errors": []}


def test_ranked_first_counts_only_judgements_with_a_ranking(we, tmp_path: Path) -> None:
    labels = {"A": "haiku", "B": "opus"}
    run = _summary_run(tmp_path, [
        {"cost": 1.0, "labels": labels, "verdict": {"pages": [_score("A"), _score("B")], "ranking": ["B", "A"]}},
        {"cost": 0.0, "labels": labels, "error": "parse"},
    ])
    rows = {line.split(" | ")[0].strip("| "): line for line in we.summarise(run, None).splitlines()
            if line.startswith(("| haiku", "| opus"))}
    assert "| 1/1 |" in rows["opus"] and "| 0/1 |" in rows["haiku"]


def test_a_judged_arm_missing_from_pages_is_ignored(we, tmp_path: Path) -> None:
    labels = {"A": "haiku", "B": "opus-v1"}  # opus-v1 has no rows in pages.jsonl
    run = _summary_run(tmp_path, [
        {"cost": 1.0, "labels": labels, "verdict": {"pages": [_score("A"), _score("B")], "ranking": ["B", "A"]}},
    ])
    out = we.summarise(run, None)
    assert "| haiku |" in out and "| opus-v1 |" not in out


def test_judge_stage_refuses_to_run_without_an_existing_run(we, tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.setattr(we, "OUT_BASE", tmp_path / "out")
    monkeypatch.setattr(we, "stage_judge", lambda *a, **k: pytest.fail("judged an empty run"))
    for argv in (["--stage", "judge"], ["--stage", "judge", "--run", str(tmp_path / "missing")]):
        monkeypatch.setattr(sys, "argv", ["wiki_eval.py", *argv])
        with pytest.raises(SystemExit) as e:
            we.main()
        assert e.value.code == 2
    assert not (tmp_path / "out").exists() and not (tmp_path / "missing").exists()
