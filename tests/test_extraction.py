"""Extraction candidates become work only when approved (auditor/extraction.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from auditor import extraction as ex
from auditor.agent_pack import (
    AgentCodingPack,
    list_packs,
    load_pack,
    pack_lock,
    rebuild_index,
    update_pack_status,
    write_pack,
)


@pytest.fixture(autouse=True)
def _no_rag(monkeypatch):
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")


def _cand(cid: str, units: list[tuple[str, str, str]], **kw) -> dict:
    return {"id": cid, "title": f"Extract {units[0][2]}", "extraction_score": 0.9,
            "repos_involved": sorted({r for r, _, _ in units}),
            "source_units": [{"repo": r, "path": f"/home/smk/mirrors/{r}/{p}", "symbols": [s]} for r, p, s in units],
            **kw}


SHA = [("argus-atlas", "src/argus_atlas/hashing.py", "sha256_bytes"),
       ("argus-mint", "src/argus_mint/cards.py", "sha256_hex"),
       ("argus-curator", "src/argus_curator/consent.py", "_sha256"),
       ("argus-echo", "engines/qwen/tests/test_checkpoints.py", "_sha")]
NOW = [("agent-composer", "app/util.py", "_now"), ("argus-forge", "src/f/t.py", "_now")]


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """A reports dir with two candidates, mirrors holding the copies, and a decisions file."""
    out, mirrors = tmp_path / "reports", tmp_path / "mirrors"
    out.mkdir()
    for repo, path, sym in SHA + NOW:
        f = mirrors / repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"def {sym}(b):\n    return b\n")
    cands = [_cand("ext-shar-005", SHA), _cand("ext-shar-002", NOW)]
    (out / "extraction_candidates.yaml").write_text(yaml.safe_dump({"candidates": cands}))
    paths = {r: str(mirrors / r) for r, _, _ in SHA + NOW}
    return {"out": out, "mirrors": mirrors, "paths": paths, "decisions": tmp_path / "rules" / "d.json"}


def _approve(w: dict, cid: str = "ext-shar-005", **kw) -> dict:
    return ex.decide(w["out"], cid, "approved", repo_paths=w["paths"], path=w["decisions"], **kw)


# ── ids ──────────────────────────────────────────────────────────────────────

def test_ids_follow_the_units_not_the_rank(tmp_path: Path) -> None:
    first = [_cand("ext-shar-001", SHA), _cand("ext-shar-002", NOW)]
    ex.stabilize_ids(first, tmp_path)
    ids = {c["title"]: c["id"] for c in first}
    assert all(i.startswith("ext-") and "shar" not in i for i in ids.values())
    # next night: ranked the other way round, positional ids swapped, one copy migrated away
    second = [_cand("ext-shar-001", NOW), _cand("ext-shar-002", SHA[:3])]
    ex.stabilize_ids(second, tmp_path)
    assert {c["title"]: c["id"] for c in second} == ids


def test_a_decision_keeps_its_id_and_a_different_cluster_gets_a_new_one(tmp_path: Path) -> None:
    decisions = {"ext-shar-005": {"status": "approved", "all_units": ex.units_of(_cand("x", SHA))}}
    cands = [_cand("ext-shar-009", SHA[1:]), _cand("ext-shar-005", NOW)]
    ex.stabilize_ids(cands, tmp_path, decisions)
    assert cands[0]["id"] == "ext-shar-005"
    assert cands[1]["id"] not in ("ext-shar-005", "ext-shar-009")
    assert json.loads((tmp_path / ex.IDS_FILE).read_text())["ids"]["ext-shar-005"]


def test_the_same_units_twice_get_distinct_ids(tmp_path: Path) -> None:
    cands = [_cand("a", NOW), _cand("b", NOW)]
    ex.stabilize_ids(cands, tmp_path)
    assert cands[0]["id"] != cands[1]["id"]


def test_units_are_repo_relative_and_tolerate_odd_shapes() -> None:
    c = {"source_units": [{"repo": "r", "path": "/m/r/a.py", "symbols": "parse"}, "junk",
                          {"repo": "r", "path": "/m/r/a.py", "symbols": ["parse"]}, {"path": "no-repo.py"}]}
    assert ex.units_of(c) == [{"repo": "r", "path": "a.py", "symbol": "parse"}]


# ── decisions ────────────────────────────────────────────────────────────────

def test_approving_writes_a_pack_listing_the_suggested_copies(world: dict) -> None:
    out = _approve(world, target_module="argus_cortex.hashing", note="atlas's is canonical")
    assert out == {"id": "ext-shar-005", "decision": "approved", "pack_id": "pack-ext-shar-005",
                   "status": "open", "left": 3, "copies": 3, "done": False}
    pack = load_pack(world["out"], "pack-ext-shar-005")
    assert pack["kind"] == "extraction" and pack["repo"]["name"] == "argus-cortex"
    copies = pack["governance"]["extraction"]["copies"]
    assert {c["repo"] for c in copies} == {"argus-atlas", "argus-mint", "argus-curator"}  # the test helper is left out
    assert "argus_cortex.hashing" in pack["narrative"]["description"]
    assert "atlas's is canonical" in pack["narrative"]["description"]
    assert [e["id"] for e in list_packs(world["out"], kind="extraction")] == ["pack-ext-shar-005"]
    saved = ex.load_decisions(world["decisions"])["ext-shar-005"]
    assert saved["status"] == "approved" and len(saved["all_units"]) == 4


def test_progress_counts_a_removed_copy_as_gone_and_an_unreadable_one_as_unknown(world: dict) -> None:
    (world["mirrors"] / "argus-mint/src/argus_mint/cards.py").write_text("from argus_cortex.hashing import sha256\n")
    (world["mirrors"] / "argus-curator/src/argus_curator/consent.py").write_text("def broken(:\n")
    _approve(world)
    states = {c["repo"]: c["state"] for c in
              load_pack(world["out"], "pack-ext-shar-005")["governance"]["extraction"]["copies"]}
    assert states == {"argus-atlas": "left", "argus-mint": "gone", "argus-curator": "unknown"}


def test_chosen_units_must_belong_to_the_candidate_and_reach_outside_the_target(world: dict) -> None:
    with pytest.raises(ValueError, match="not units"):
        _approve(world, units=["argus-atlas:nope.py:x"])
    with pytest.raises(ValueError, match="outside the target"):
        _approve(world, units=["argus-atlas:src/argus_atlas/hashing.py:sha256_bytes"], target_repo="argus-atlas")
    with pytest.raises(ValueError, match="module"):
        _approve(world, target_module="argus_cortex; rm -rf")
    assert ex.decide(world["out"], "ext-nope", "approved", path=world["decisions"]) is None
    assert not world["decisions"].exists()


def test_dismissing_closes_the_pack_and_approving_again_reopens_it(world: dict) -> None:
    _approve(world)
    out = ex.decide(world["out"], "ext-shar-005", "dismissed", note="different hash inputs", path=world["decisions"])
    assert out["status"] == "wontfix"
    assert load_pack(world["out"], "pack-ext-shar-005")["status"] == "wontfix"
    _approve(world)
    assert load_pack(world["out"], "pack-ext-shar-005")["status"] == "open"


def test_a_decision_never_moves_a_pack_an_agent_holds(world: dict) -> None:
    _approve(world)
    update_pack_status(world["out"], "pack-ext-shar-005", "claimed", "agent-1")
    with pytest.raises(ex.DecisionConflict, match="claimed"):
        ex.decide(world["out"], "ext-shar-005", "dismissed", path=world["decisions"])
    with pytest.raises(ex.DecisionConflict):
        ex.withdraw(world["out"], "ext-shar-005", path=world["decisions"])
    assert load_pack(world["out"], "pack-ext-shar-005")["status"] == "claimed"
    assert ex.load_decisions(world["decisions"])["ext-shar-005"]["status"] == "approved"
    # re-approving (say, a new note) keeps the claim
    _approve(world, note="also argus-realms")
    assert load_pack(world["out"], "pack-ext-shar-005")["status"] == "claimed"


def test_withdrawing_makes_it_a_proposal_again(world: dict) -> None:
    _approve(world)
    assert ex.withdraw(world["out"], "ext-shar-005", path=world["decisions"])["pack_status"] == "obsolete"
    assert ex.load_decisions(world["decisions"]) == {}
    assert ex.withdraw(world["out"], "ext-shar-005", path=world["decisions"]) is None


def test_a_decision_outlives_its_candidate(world: dict) -> None:
    _approve(world)
    (world["out"] / "extraction_candidates.yaml").write_text(yaml.safe_dump({"candidates": []}))
    assert ex.decide(world["out"], "ext-shar-005", "approved", path=world["decisions"])["status"] == "open"


# ── nightly sync ─────────────────────────────────────────────────────────────

def _old_pack(out: Path, pid: str, status: str = "open") -> None:
    pack = AgentCodingPack(work_item_id=pid, kind="extraction", priority={"severity": "HIGH", "score": 0.9},
                           repo={"name": "argus-atlas"}, location={}, narrative={"title": pid}, impact={},
                           governance={}, verification={}, repair={}, provenance={})
    with pack_lock(out):
        write_pack(pack, out)
        rebuild_index(out)
    if status != "open":
        update_pack_status(out, pid, status, "agent")


def test_sync_closes_unapproved_open_packs_and_refreshes_approved_ones(world: dict) -> None:
    out = world["out"]
    _old_pack(out, "pack-ext-shar-101")
    _old_pack(out, "pack-ext-shar-102", "claimed")
    _approve(world)
    (world["mirrors"] / "argus-mint/src/argus_mint/cards.py").unlink()
    res = ex.sync_packs(out, world["paths"], path=world["decisions"])
    assert res == {"refreshed": 1, "closed": 1, "resolved": 0, "left": {"pack-ext-shar-005": 2}}
    assert load_pack(out, "pack-ext-shar-101")["status"] == "obsolete"
    assert load_pack(out, "pack-ext-shar-102")["status"] == "claimed"  # someone's work: untouched
    assert load_pack(out, "pack-ext-shar-005")["status"] == "open"
    assert load_pack(out, "pack-ext-shar-005")["governance"]["extraction"]["left"] == 2


def test_sync_with_no_decisions_file_only_closes(world: dict) -> None:
    _old_pack(world["out"], "pack-ext-shar-101")
    assert ex.sync_packs(world["out"], {}, path=world["decisions"])["closed"] == 1


def test_a_malformed_decisions_file_raises_instead_of_reading_as_none(tmp_path: Path) -> None:
    bad = tmp_path / "d.json"
    bad.write_text(json.dumps({"decisions": ["not", "a", "map"]}))
    with pytest.raises(ValueError):
        ex.load_decisions(bad)


def test_sync_resolves_a_pack_once_every_copy_is_gone_and_never_on_unknown(world: dict) -> None:
    _approve(world)
    update_pack_status(world["out"], "pack-ext-shar-005", "in_progress", "PRs open")
    for repo, path, _ in SHA[:2]:
        (world["mirrors"] / repo / path).write_text("from argus_cortex.hashing import sha256\n")
    (world["mirrors"] / SHA[2][0] / SHA[2][1]).write_text("def broken(:\n")  # unreadable: unknown
    assert ex.sync_packs(world["out"], world["paths"], path=world["decisions"])["resolved"] == 0
    assert load_pack(world["out"], "pack-ext-shar-005")["status"] == "in_progress"
    (world["mirrors"] / SHA[2][0] / SHA[2][1]).write_text("from argus_cortex.hashing import sha256\n")
    assert ex.sync_packs(world["out"], world["paths"], path=world["decisions"])["resolved"] == 1
    pack = load_pack(world["out"], "pack-ext-shar-005")
    assert pack["status"] == "resolved" and "every copy is gone" in pack["status_note"]
