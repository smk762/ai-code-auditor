"""scripts/wiki_eval.py: JSONL files stay appendable; stray entries don't abort judging (batch-4 packs)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_wiki_eval_packs import repo, we  # noqa: F401  (fixtures)


def _judge_call(judged: list):
    def call(cl, model, effort, *, system=None, content, max_tokens, schema=None):
        judged.append(content)
        return {"text": json.dumps({"pages": []}), "latency_s": 1.0, "cost": 1.0}
    return call


def _two_arms(run: Path, repo_name: str, we) -> None:  # noqa: F811
    for arm in ("haiku", "opus"):
        d = run / "pages" / repo_name / arm
        d.mkdir(parents=True, exist_ok=True)
        (d / "overview.md").write_text("# overview\n")


def test_pages_jsonl_ends_with_a_newline_so_appends_stay_parseable(we, repo, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(we, "load_notes", lambda repo: [{"path": "app.py"}])
    monkeypatch.setattr(we, "repo_context", lambda repo, notes: "ctx")
    monkeypatch.setattr(we, "structure_block", lambda repo: "struct")
    monkeypatch.setattr(we, "check_citations", lambda text, repo: {"valid": 0, "total": 0, "matched": 0, "named": 0})
    monkeypatch.setattr(we, "call", lambda *a, **k: {"text": "page", "latency_s": 1.0, "cost": 0.5})
    run = tmp_path / "run"
    we.stage_pages(["svc"], ["haiku"], run, concurrency=2)
    text = (run / "pages.jsonl").read_text()
    assert text.endswith("\n")
    with (run / "pages.jsonl").open("a") as fh:  # a later append-per-record writer
        fh.write(json.dumps({"repo": "svc", "late": True}) + "\n")
    recs = [json.loads(line) for line in (run / "pages.jsonl").read_text().splitlines() if line.strip()]
    assert len(recs) == len(we.PAGES) + 1


def test_judgements_jsonl_ends_with_a_newline(we, repo, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    run = tmp_path / "run"
    _two_arms(run, "svc", we)
    monkeypatch.setattr(we, "full_source", lambda repo: "src")
    monkeypatch.setattr(we, "call", _judge_call([]))
    results = we.stage_judge(run, concurrency=1)
    text = (run / "judgements.jsonl").read_text()
    assert text.endswith("\n") and len(text.splitlines()) == len(results) == 1


def test_unknown_repos_and_stray_files_under_pages_are_skipped(we, repo, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    run = tmp_path / "run"
    _two_arms(run, "svc", we)
    _two_arms(run, "renamed-repo", we)  # no longer in the registry
    (run / "pages" / "notes.txt").write_text("stray")
    (run / "pages" / "svc" / "README").write_text("stray file among the arms")
    monkeypatch.setattr(we, "full_source", lambda repo: "src")
    judged: list = []
    monkeypatch.setattr(we, "call", _judge_call(judged))
    results = we.stage_judge(run, concurrency=1)
    assert [(r["repo"], r["page"]) for r in results] == [("svc", "overview")] and len(judged) == 1
    assert sorted(results[0]["labels"].values()) == ["haiku", "opus"]
