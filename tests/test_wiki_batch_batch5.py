"""wiki/batch.py: a crash before the ledger write is charged later; concurrent runners keep each
other's batches (batch-5 packs). Fake client and ledger only: no paid API calls."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tests.test_wiki import FakeClient, _req
from wiki.batch import Runner, StageStats


class _Ledger:
    def __init__(self, die: bool = False):
        self.die, self.charges = die, []

    def remaining(self, lane):
        return 100.0

    def record_usage(self, lane, model, usage, *, batch=False, note=""):
        if self.die:
            raise SystemExit("killed between storing the reply and charging it")
        get = usage.get if isinstance(usage, dict) else (lambda k, d=0: getattr(usage, k, d))
        self.charges.append((lane, model, get("input_tokens", 0), get("output_tokens", 0), batch))
        return 0.25


def test_a_reply_stored_but_never_charged_is_charged_on_the_next_run(tmp_path: Path, monkeypatch) -> None:
    fake = FakeClient(reply=lambda p: "answer")
    monkeypatch.setattr("wiki.batch.get_ledger", lambda: _Ledger(die=True))
    with pytest.raises(SystemExit):
        Runner(tmp_path, mode="sync", client=fake).run("t", [_req(1)])
    assert fake.streamed == 1

    ledger = _Ledger()
    monkeypatch.setattr("wiki.batch.get_ledger", lambda: ledger)
    results, st = Runner(tmp_path, mode="sync", client=fake).run("t", [_req(1)])
    assert fake.streamed == 1 and st.reused == 1  # the reply is reused, never paid for twice
    assert len(ledger.charges) == 1  # was 0: the paid reply never reached the ledger
    assert ledger.charges[0][2:4] == (2000, 500)
    assert results[_req(1).custom_id].cost == st.cost_usd == 0.25

    Runner(tmp_path, mode="sync", client=fake).run("t", [_req(1)])
    assert len(ledger.charges) == 1  # charged once, not on every reuse


def test_a_runner_never_drops_a_batch_another_runner_just_added(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("wiki.batch.get_ledger", lambda: _Ledger())
    fake = FakeClient(reply=lambda p: "answer")
    a = Runner(tmp_path, client=fake, poll_s=0)
    b = Runner(tmp_path, client=fake, poll_s=0)
    for r in (a, b):
        r._harvest = lambda *args, **kw: False  # submitted, not ended yet
    other = threading.Thread(target=lambda: b._run_batch("t", [_req(2)], StageStats()))
    real_load = a._load_state
    fired = []

    def load_then_let_the_other_runner_write():
        state = real_load()
        if not fired:  # a has read the state; b submits and saves before a writes it back
            fired.append(1)
            other.start()
            other.join(timeout=0.5)
        return state

    monkeypatch.setattr(a, "_load_state", load_then_let_the_other_runner_write)
    a._run_batch("t", [_req(1)], StageStats())
    other.join(timeout=10)
    assert not other.is_alive()
    monkeypatch.setattr(a, "_load_state", real_load)
    # was {_req(1)}: b's batch was overwritten, dropped from in_flight, and would be paid for again
    assert a.in_flight() == {_req(1).custom_id, _req(2).custom_id}
