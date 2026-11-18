"""Regression tests for audit-pack fixes in wiki/batch.py, wiki/findings.py, wiki/notes.py and wiki/sources.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor.spend_ledger import get_ledger
from tests.test_wiki import FakeClient, _req
from wiki.batch import Runner


# ── wiki/batch.py ────────────────────────────────────────────────────────────

class _Gone(Exception):
    status_code = 404


def test_harvest_rerun_does_not_charge_entries_already_recorded(tmp_path: Path) -> None:
    fake = FakeClient(reply=lambda p: "answer")
    reqs = [_req(1), _req(2)]
    runner = Runner(tmp_path, client=fake, poll_s=0)
    runner._harvest = lambda *a, **k: False  # submit, then die before harvesting
    runner.run("t", reqs)
    bid = next(iter(json.loads((tmp_path / "wiki" / "_batches.json").read_text())["batches"]))
    entries = list(fake.messages.batches.results(bid))

    calls = {"n": 0}

    def interrupted(_bid):
        for e in entries:
            if calls["n"] == 1:
                raise ConnectionError("stream dropped")
            calls["n"] += 1
            yield e

    fake.messages.batches.results = interrupted
    with pytest.raises(ConnectionError):
        Runner(tmp_path, client=fake, poll_s=0).harvest_pending()
    after_first = get_ledger().spent("wiki")
    assert after_first > 0

    fake.messages.batches.results = lambda _bid: iter(entries)  # the rerun streams everything again
    results, st = Runner(tmp_path, client=fake, poll_s=0).run("t", reqs)
    assert all(r.ok for r in results.values()) and st.submitted == 0
    one = after_first  # exactly one entry had been charged before the interruption
    assert get_ledger().spent("wiki") == pytest.approx(2 * one)


def test_a_batch_that_is_gone_is_dropped_instead_of_wedging_every_run(tmp_path: Path) -> None:
    fake = FakeClient(reply=lambda p: "answer")
    runner = Runner(tmp_path, client=fake, poll_s=0)
    runner._harvest = lambda *a, **k: False
    runner.run("t", [_req(1)])
    state = tmp_path / "wiki" / "_batches.json"
    assert json.loads(state.read_text())["batches"]

    retrieve = fake.messages.batches.retrieve

    def gone(bid):
        if bid == "msgbatch_0":  # the stale one recorded in state
            raise _Gone("batch not found")
        return retrieve(bid)

    fake.messages.batches.retrieve = gone
    results, st = Runner(tmp_path, client=fake, poll_s=0).run("t", [_req(2)])
    assert results[_req(2).custom_id].ok and st.submitted == 1
    assert list(json.loads(state.read_text())["batches"]) == []


def test_a_transient_harvest_error_keeps_the_batch(tmp_path: Path) -> None:
    fake = FakeClient(reply=lambda p: "answer")
    runner = Runner(tmp_path, client=fake, poll_s=0)
    runner._harvest = lambda *a, **k: False
    runner.run("t", [_req(1)])

    def flaky(_bid):
        raise ConnectionError("timeout")

    fake.messages.batches.retrieve = flaky
    with pytest.raises(ConnectionError):
        Runner(tmp_path, client=fake, poll_s=0).harvest_pending()
    assert json.loads((tmp_path / "wiki" / "_batches.json").read_text())["batches"]


# ── wiki/sources.py ──────────────────────────────────────────────────────────

def test_module_level_conditional_imports_are_not_lazy() -> None:
    from wiki.sources import file_structure

    src = (
        "import sys\n"
        "from typing import TYPE_CHECKING\n"
        "try:\n    import tomllib\nexcept ImportError:\n    import tomli as tomllib\n"
        "if sys.version_info >= (3, 11):\n    from . import fast\n"
        "if TYPE_CHECKING:\n    from pkg.types import Thing\n"
        "def f():\n    import yaml\n    return yaml\n"
    )
    s = file_structure(Path("m.py"), src)
    assert s["imports"] == ["sys", "typing: TYPE_CHECKING", "tomllib", "tomli", ".: fast",
                            "pkg.types: Thing (type checking only)"]
    assert s["lazy_imports"] == ["yaml"]
