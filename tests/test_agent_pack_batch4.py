"""Regression tests for agent_pack audit packs, batch 4 (index repair, path safety, lifecycle, locking)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor import agent_pack
from auditor.agent_pack import (
    AgentCodingPack,
    emit_packs_from_findings,
    finding_from_violation,
    index_path,
    pack_file_path,
    packs_dir,
    read_code_snippet,
    rebuild_index,
    upsert_index_entries,
    write_pack,
)
from auditor.contracts import Finding, RuleViolation


@pytest.fixture(autouse=True)
def _no_rag(monkeypatch):
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")


def _pack(pid: str = "pack-x-1", **over) -> AgentCodingPack:
    base: dict = {"work_item_id": pid, "kind": "bug", "priority": {"severity": "HIGH"}, "repo": {"name": "x"},
                  "location": {"file_path": "a.py", "line": 1, "evidence": "", "code_snippet": ""},
                  "narrative": {"title": "t", "description": "d", "recommendation": "r"}}
    base.update(over)
    return AgentCodingPack(**base)


def _ids(payload: dict) -> list[str]:
    return [e["id"] for e in payload["packs"]]


# --- bbbfe08020: one malformed pack file must not abort rebuild_index ---------------------------

@pytest.mark.parametrize("raw", [
    b"\xff\xfe\x00 not utf-8",                                       # UnicodeDecodeError
    b"work_item_id: pack-bad\npriority: not-a-mapping\n",            # from_dict raises
    b"work_item_id: pack-bad\nlocation: {line: '12-14'}\n",          # _index_entry raises
])
def test_rebuild_index_skips_a_malformed_pack(tmp_path: Path, raw: bytes) -> None:
    write_pack(_pack("pack-good"), tmp_path)
    (packs_dir(tmp_path) / "pack-bad.yaml").write_bytes(raw)
    payload = rebuild_index(tmp_path)
    assert _ids(payload) == ["pack-good"]
    assert _ids(json.loads(index_path(tmp_path).read_text())) == ["pack-good"]


# --- 78665272ba: an index of the wrong shape is rebuilt by upsert -------------------------------

@pytest.mark.parametrize("raw", ["[]", "null", '"x"', '{"packs": ["pack-a"]}', '{"packs": 5}'])
def test_upsert_rebuilds_an_index_of_the_wrong_shape(tmp_path: Path, raw: str) -> None:
    write_pack(_pack("pack-a"), tmp_path)
    new = _pack("pack-b")
    write_pack(new, tmp_path)
    index_path(tmp_path).write_text(raw)
    upsert_index_entries(tmp_path, [new])
    assert sorted(_ids(json.loads(index_path(tmp_path).read_text()))) == ["pack-a", "pack-b"]


# --- 06a7ee9f08: the snippet reader stays inside the repo ---------------------------------------

def test_snippet_reader_refuses_paths_outside_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("inside\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET\n")
    (repo / "link.txt").symlink_to(secret)

    assert "inside" in read_code_snippet(repo, "pkg/a.py", 1)
    assert "inside" in read_code_snippet(repo, str(repo / "pkg" / "a.py"), 1)  # absolute, in the repo
    for hostile in ("../secret.txt", str(secret), "pkg/../../secret.txt", "link.txt"):
        assert read_code_snippet(repo, hostile, 1) == "", hostile


# --- 7fb8684665: draft_pr_url survives a re-emit on every status path ---------------------------

def test_draft_pr_url_survives_an_explicit_status_reemit(tmp_path: Path) -> None:
    first = _pack(repair={"draft_pr_url": "https://example.test/pr/1"})
    first.apply_status("claimed", "agent")
    write_pack(first, tmp_path)
    again = _pack()
    again.apply_status("in_progress", "conductor")
    write_pack(again, tmp_path)
    assert again.repair.get("draft_pr_url") == "https://example.test/pr/1"


def test_draft_pr_url_survives_on_a_never_transitioned_pack(tmp_path: Path) -> None:
    write_pack(_pack(repair={"draft_pr_url": "https://example.test/pr/2"}), tmp_path)
    again = _pack()
    write_pack(again, tmp_path)
    assert again.repair.get("draft_pr_url") == "https://example.test/pr/2"


# --- 63ef9eddd3: dedupe and recurrence lookups run under pack_lock ------------------------------

def test_emit_decides_dedupe_and_recurrence_under_the_lock(tmp_path: Path, monkeypatch) -> None:
    held: dict[str, bool] = {}
    real_merge, real_link = agent_pack.merge_into_existing, agent_pack.link_recurrences

    def _depth() -> bool:
        return getattr(agent_pack._index_lock_depth, "n", 0) > 0

    def merge(packs, out):
        held["merge"] = _depth()
        return real_merge(packs, out)

    def link(packs, out):
        held["link"] = _depth()
        return real_link(packs, out)

    monkeypatch.setattr(agent_pack, "merge_into_existing", merge)
    monkeypatch.setattr(agent_pack, "link_recurrences", link)
    f = Finding(id="", type="bug", severity="HIGH", repo="x", file_path="a.py", line=1, title="t",
                description="d", evidence="e", recommendation="r", source="llm")
    emit_packs_from_findings([f], output_dir=tmp_path, run_id="r1")
    assert held == {"merge": True, "link": True}


# --- 34c270341b: hashed fallback names can't collide with literal safe ids ----------------------

@pytest.mark.parametrize("lossy", ["a b", "", "x" * 130, "a/b"])
def test_hashed_file_name_is_not_a_literal_id(tmp_path: Path, lossy: str) -> None:
    hashed = pack_file_path(tmp_path, lossy)
    literal = hashed.stem  # what an attacker would pick as an id
    assert pack_file_path(tmp_path, literal) != hashed


# --- 8424b6623e: "repo/path:line" splits the line off the path ----------------------------------

@pytest.mark.parametrize("entities", [["repo"], []])
def test_slash_form_violation_path_splits_the_line(entities: list[str]) -> None:
    f = finding_from_violation(RuleViolation(rule_name="r", violating_path="repo/pkg/file.py:123",
                                             entities=entities, evidence=""))
    assert (f.repo, f.file_path, f.line) == ("repo", "pkg/file.py", 123)
