"""wiki/batch.py: requests already paid for are never paid for again (batch-4 packs)."""
from __future__ import annotations

import json
from pathlib import Path

from auditor import spend_ledger
from tests.test_wiki import FakeClient, _req
from wiki.batch import Runner


def test_requests_in_a_still_running_batch_are_not_resubmitted(tmp_path: Path) -> None:
    fake = FakeClient(reply=lambda p: "answer")
    first = Runner(tmp_path, client=fake, poll_s=0)
    first._harvest = lambda *a, **k: False  # submitted, not ended within max_wait_s
    first.run("t", [_req(1), _req(2)])
    assert len(fake.batches_created) == 1

    still_running = Runner(tmp_path, client=fake, poll_s=0)
    still_running._harvest = lambda *a, **k: False  # the earlier batch still hasn't ended
    results, st = still_running.run("t", [_req(1), _req(2), _req(3)])
    resubmitted = [r["custom_id"] for b in fake.batches_created[1:] for r in b]
    assert resubmitted == [_req(3).custom_id]  # only the new request is paid for
    assert st.submitted == 1 and st.failed == 3  # 1 and 2 pending, 3 not harvested here
    assert "pending" in results[_req(1).custom_id].error

    # nothing new: nothing submitted, and the pending ones are reported, not dropped
    results, st = still_running.run("t", [_req(1)])
    assert len(fake.batches_created) == 2 and st.submitted == 0 and not results[_req(1).custom_id].ok

    # once the batches end, a rerun harvests them and resubmits nothing
    results, st = Runner(tmp_path, client=fake, poll_s=0).run("t", [_req(1), _req(2), _req(3)])
    assert all(r.ok for r in results.values()) and st.submitted == 0 and len(fake.batches_created) == 2
    assert json.loads((tmp_path / "wiki" / "_batches.json").read_text())["batches"] == {}


def test_a_ledger_failure_keeps_the_paid_sync_reply_and_the_stage_going(tmp_path: Path, monkeypatch) -> None:
    fake = FakeClient(reply=lambda p: "answer")

    class _BrokenLedger:
        def remaining(self, lane):
            return 100.0

        def record_usage(self, *a, **k):
            raise OSError("ledger is read-only")

    monkeypatch.setattr("wiki.batch.get_ledger", lambda: _BrokenLedger())
    results, st = Runner(tmp_path, mode="sync", client=fake).run("t", [_req(1), _req(2)])
    assert fake.streamed == 2 and all(r.ok for r in results.values()) and st.failed == 0

    monkeypatch.setattr("wiki.batch.get_ledger", spend_ledger.get_ledger)
    results, st = Runner(tmp_path, mode="sync", client=fake).run("t", [_req(1), _req(2)])
    assert fake.streamed == 2 and st.reused == 2  # stored before the ledger failed: never paid for again


def test_the_recorded_cost_is_stored_with_the_reply(tmp_path: Path) -> None:
    fake = FakeClient(reply=lambda p: "answer")
    runner = Runner(tmp_path, mode="sync", client=fake)
    results, _ = runner.run("t", [_req(1)])
    stored = runner._stored(_req(1).custom_id)
    assert stored is not None and stored.cost == results[_req(1).custom_id].cost > 0
