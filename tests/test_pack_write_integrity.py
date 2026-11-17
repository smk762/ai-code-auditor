"""Pack writers can't overwrite each other: GC vs a claim, two claims, and readers vs half a file."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import agent_pack, pack_gc
from auditor.agent_pack import (
    PackStatusConflict, ingest_packs_to_rag, load_pack, rebuild_index, update_pack_status, write_pack,
)
from tests.test_agent_pack import _client, _mk_pack


def _seed(out: Path, *ids: str) -> None:
    for pid in ids:
        write_pack(_mk_pack(pid), out)
    rebuild_index(out)


def test_gc_never_overwrites_a_claim_that_lands_mid_apply(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, "pack-a")
    loaded, real_load = threading.Event(), agent_pack.load_pack

    def slow_load(out, pid):
        data = real_load(out, pid)
        if threading.current_thread().name == "gc":
            loaded.set()
            threading.Event().wait(0.5)  # the window between GC's check and its write
        return data

    monkeypatch.setattr(agent_pack, "load_pack", slow_load)
    closure = pack_gc.Closure("pack-a", "argus-cortex", "x.py", "t", "nightly", "not_rescanned", "gone")
    gc = threading.Thread(target=pack_gc.apply, args=(tmp_path, [closure]), name="gc")
    gc.start()
    assert loaded.wait(5)
    update_pack_status(tmp_path, "pack-a", "claimed", "agent A")  # waits for GC's lock, then lands
    gc.join(5)
    assert real_load(tmp_path, "pack-a")["status"] == "claimed"  # without the lock: obsolete


def test_a_second_claim_gets_409_unless_forced(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, "pack-a")
    client = _client(tmp_path, monkeypatch)
    first = client.post("/audit/packs/pack-a/status", json={"status": "claimed", "note": "agent A"})
    assert first.status_code == 200
    second = client.post("/audit/packs/pack-a/status", json={"status": "claimed", "note": "agent B"})
    assert second.status_code == 409
    assert second.json()["detail"]["status"] == "claimed" and second.json()["detail"]["status_note"] == "agent A"
    assert load_pack(tmp_path, "pack-a")["status_note"] == "agent A"  # B wrote nothing
    forced = client.post("/audit/packs/pack-a/status", json={"status": "claimed", "note": "B", "force": True})
    assert forced.status_code == 200
    # explicit compare-and-set on any transition
    cas = client.post("/audit/packs/pack-a/status", json={"status": "resolved", "expect_status": ["in_progress"]})
    assert cas.status_code == 409


def test_concurrent_claims_have_exactly_one_winner(tmp_path: Path) -> None:
    _seed(tmp_path, "pack-a")
    wins: list[str] = []

    def claim(name: str) -> None:
        try:
            update_pack_status(tmp_path, "pack-a", "claimed", name, expect=agent_pack.CLAIMABLE_STATUSES)
            wins.append(name)
        except PackStatusConflict:
            pass

    threads = [threading.Thread(target=claim, args=(f"agent-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(wins) == 1 and load_pack(tmp_path, "pack-a")["status_note"] == wins[0]


@pytest.mark.parametrize("path", ["gc", "emit", "guard"])
def test_writers_patch_the_index_instead_of_rebuilding(tmp_path: Path, monkeypatch, path: str) -> None:
    from auditor import class_packs
    from auditor.contracts import Finding

    _seed(tmp_path, "pack-a")
    monkeypatch.setattr(agent_pack, "rebuild_index", lambda *a, **k: pytest.fail("full rebuild on a write path"))
    if path == "gc":
        closure = pack_gc.Closure("pack-a", "argus-cortex", "x.py", "t", "nightly", "not_rescanned", "gone")
        assert pack_gc.apply(tmp_path, [closure]) == 1
    elif path == "emit":
        f = Finding(id="f1", repo="argus-cortex", file_path="a.py", line=1, severity="HIGH", type="bug", title="t",
                    description="d", evidence="e", recommendation="r", source="llm")
        assert agent_pack.emit_packs_from_findings([f], output_dir=tmp_path)
    else:
        pk = _mk_pack("pack-class-x")
        pk.kind = class_packs.KIND
        write_pack(pk, tmp_path)
        class_packs.record_guard(tmp_path, "pack-class-x", repo="r", commit="abcdef1", proof_test="tests/t.py",
                                 fixtures=[])
    entries = {e["id"] for e in agent_pack.list_packs(tmp_path, limit=100)}
    assert "pack-a" in entries


def test_rag_send_names_each_doc_once(tmp_path: Path, monkeypatch) -> None:
    sent: list[list[str]] = []
    monkeypatch.setattr("auditor.rag_client.post_documents", lambda docs, **kw: sent.append([d["id"] for d in docs]) or {})
    monkeypatch.setattr("auditor.rag_client.record_sent", lambda *a, **k: None)
    monkeypatch.setattr("auditor.rag_client.recorded_ids", lambda *a, **k: set())
    monkeypatch.setattr("auditor.rag_client.retire", lambda *a, **k: {})
    a1, a2 = _mk_pack("pack-a", title="first"), _mk_pack("pack-a", title="second")
    ingest_packs_to_rag([a1, a2], output_dir=tmp_path)
    assert sent and all(len(ids) == len(set(ids)) for ids in sent)
