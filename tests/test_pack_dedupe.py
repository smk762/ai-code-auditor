"""Pack dedupe: re-found issues reuse their pack; existing duplicates close as wontfix."""
from __future__ import annotations

from pathlib import Path

import pytest

from auditor import agent_pack
from auditor.agent_pack import (
    dedupe_packs,
    emit_packs_from_findings,
    list_packs,
    load_pack,
    update_pack_status,
)
from auditor.contracts import Finding
from auditor.finding_match import reworded_score


@pytest.fixture(autouse=True)
def _no_rag(monkeypatch):
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")


def _f(title: str, *, file: str = "/m/x/app.py", line: int = 10, n: int = 0, sev: str = "HIGH") -> Finding:
    return Finding(id=f"llm:u{n}:{line}", type="bug", severity=sev, repo="x", file_path=file, line=line,
                   title=title, description="d", evidence="e", recommendation="r", source="llm")


def _emit(out: Path, findings: list[Finding], run: str, kind: str = "nightly") -> list[str]:
    return [p.work_item_id for p in emit_packs_from_findings(findings, output_dir=out, kind=kind, run_id=run,
                                                             public_base_url="http://audit:8765")]


def _ids(out: Path, **kw) -> list[str]:
    return sorted(e["id"] for e in list_packs(out, layer="specialist", limit=1000, **kw))


def test_reworded_score() -> None:
    assert reworded_score("Model unloaded while inference running", "Model unloaded during running inference") >= 0.3
    assert reworded_score("SQL injection in search", "Missing timeout on HTTP call") == 0
    # 0.2–0.3 only counts within 10 lines, and only when both lines are known
    a = "Cache key ignores `strict`, so results from one strictness setting are served for the other"
    b = "Embedder check cache ignores the `strict` flag"
    assert 0.2 <= reworded_score(a, b, 103, 103) < 0.3
    assert reworded_score(a, b, 103, 103) > 0
    assert reworded_score(a, b, 103, 300) == 0
    assert reworded_score(a, b, 0, 103) == 0


def test_reworded_refinding_reuses_pack_and_keeps_claim(tmp_path: Path) -> None:
    [first] = _emit(tmp_path, [_f("Eviction can unload the model while an inference is in flight")], "run-1")
    update_pack_status(tmp_path, first, "claimed", "agent A")

    ids = _emit(tmp_path, [_f("Eviction/unload can move the model while inference is in flight", line=12),
                           _f("Unrelated: missing timeout on HTTP call", line=80, n=1)], "run-2")
    assert ids[0] == first  # merged into the claimed pack
    assert ids[1] != first
    pack = load_pack(tmp_path, first)
    assert pack["status"] == "claimed"
    assert pack["narrative"]["title"].startswith("Eviction/unload")  # newest wording, line, snippet
    assert pack["location"]["line"] == 12
    assert pack["repair"]["slug"] == first.replace("pack-", "repair/", 1)
    assert pack["repair"]["pack_url"] == f"http://audit:8765/audit/packs/{first}"
    assert len(_ids(tmp_path)) == 2


def test_merge_rules(tmp_path: Path) -> None:
    title = "Unescaped title injected via innerHTML"
    [same_run_a] = _emit(tmp_path, [_f(title, file="/m/x/ui.js")], "run-1")
    # same run, same file, similar title: two distinct bugs as far as merging goes
    [same_run_b] = _emit(tmp_path, [_f("Unescaped summary injected via innerHTML", file="/m/x/ui.js", n=1)], "run-1")
    assert same_run_b != same_run_a

    [other_file] = _emit(tmp_path, [_f(title, file="/m/x/other.js")], "run-2")
    assert other_file not in (same_run_a, same_run_b)

    # resolved packs are not merge targets: a comeback after a fix gets a fresh open pack
    [fixed] = _emit(tmp_path, [_f("Pickle loaded with weights_only=False", file="/m/x/s3.py")], "run-1")
    update_pack_status(tmp_path, fixed, "resolved")
    [back] = _emit(tmp_path, [_f("Pickle loaded with weights_only=False", file="/m/x/s3.py")], "run-3")
    assert back != fixed and load_pack(tmp_path, back)["status"] == "open"

    # wontfix is a merge target: the decision sticks
    [declined] = _emit(tmp_path, [_f("Debug logging on by default", file="/m/x/cfg.py")], "run-1")
    update_pack_status(tmp_path, declined, "wontfix", "intended")
    [again] = _emit(tmp_path, [_f("Debug logging enabled by default", file="/m/x/cfg.py")], "run-4", kind="diff_audit")
    assert again == declined and load_pack(tmp_path, declined)["status"] == "wontfix"


def test_one_existing_pack_absorbs_one_repeat(tmp_path: Path) -> None:
    [old] = _emit(tmp_path, [_f("Model demoted while inference is still running")], "run-1")
    ids = _emit(tmp_path, [_f("Model demoted while inference still running", n=1),
                           _f("Model unloaded while inference is still running", n=2, line=11)], "run-2")
    assert ids[0] == old and ids[1] != old  # best match takes it


def test_architecture_packs_never_title_match(tmp_path: Path) -> None:
    f = _f("x: app.py:486", file="/m/x/app.py", line=486)
    [a] = _emit(tmp_path, [f], "run-1", kind="architecture")
    [b] = _emit(tmp_path, [_f("x: app.py:469", file="/m/x/app.py", line=469)], "run-2", kind="architecture")
    assert a != b


def test_dedupe_existing_packs(tmp_path: Path, monkeypatch) -> None:
    # Build duplicates the way they arose before the merge existed: run-scoped ids, no merging.
    monkeypatch.setattr(agent_pack, "merge_into_existing", lambda packs, out: {})
    [nightly] = _emit(tmp_path, [_f("Internal exception text returned to client", file="/m/x/api.py")], "run-1")
    [claimed] = _emit(tmp_path, [_f("Provider API keys stored as plain base64", file="/m/x/keys.py")], "run-1")
    update_pack_status(tmp_path, claimed, "claimed")
    [dup1] = _emit(tmp_path, [_f("Internal exception text returned to the client", file="/m/x/api.py")],
                   "diff-1", kind="diff_audit")
    [dup2] = _emit(tmp_path, [_f("Provider API keys are stored as plain base64", file="/m/x/keys.py")],
                   "diff-2", kind="diff_audit")
    # same run: kept apart even though the titles are close
    [twin] = _emit(tmp_path, [_f("Internal exception text returned to client on delete", file="/m/x/api.py", n=9)],
                   "run-1")

    plan = dedupe_packs(tmp_path, dry_run=True)
    closed = {d["id"]: d["duplicate_of"] for d in plan}
    assert closed[dup2] == claimed  # the claimed pack is always the keeper
    # dup1 repeats the run-1 api.py packs; those two are one run, so neither closes the other
    assert closed[dup1] in (nightly, twin) and set(closed) == {dup1, dup2}
    assert all(load_pack(tmp_path, d["id"])["status"] == "open" for d in plan)  # dry run changes nothing

    done = dedupe_packs(tmp_path)
    assert done == plan
    for d in done:
        pack = load_pack(tmp_path, d["id"])
        assert pack["status"] == "wontfix" and pack["status_note"] == f"duplicate of {d['duplicate_of']}"
        assert pack["governance"]["duplicate_of"] == d["duplicate_of"]
    assert load_pack(tmp_path, claimed)["status"] == "claimed"
    assert load_pack(tmp_path, twin)["status"] == "open"
    assert dedupe_packs(tmp_path) == []  # idempotent
    # an exact-id re-emit of a closed duplicate keeps it closed as a duplicate
    dup_id = next(iter(d["id"] for d in done if d["id"] == dup2))
    [same] = _emit(tmp_path, [_f("Provider API keys are stored as plain base64", file="/m/x/keys.py")],
                   "diff-2", kind="diff_audit")
    assert same == dup_id and load_pack(tmp_path, dup_id)["governance"]["duplicate_of"] == claimed

    # a later repeat merges into the keeper, never into a pack closed as its duplicate
    monkeypatch.undo()
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    [again] = _emit(tmp_path, [_f("Provider API keys stored as plain base64 text", file="/m/x/keys.py")], "run-9")
    assert again == claimed
