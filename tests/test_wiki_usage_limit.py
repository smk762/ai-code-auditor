"""wiki/batch.py: the Anthropic workspace usage limit stops a build cleanly, like the lane budget."""
from __future__ import annotations

from pathlib import Path

import pytest

from auditor.spend_ledger import BudgetExceeded
from tests.test_wiki import FakeClient, _req, ecosystem  # noqa: F401 — fixture
from wiki.batch import Runner

LIMIT = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': "
         "'You have reached your specified workspace API usage limits. You will regain access on "
         "2026-12-01 at 00:00 UTC.'}}")


class _ApiError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _refuse_batches_after(fake: FakeClient, n: int, exc: Exception) -> None:
    create = fake.messages.batches.create

    def limited(requests):
        if len(fake.batches_created) >= n:
            raise exc
        return create(requests)

    fake.messages.batches.create = limited


def test_a_usage_limited_stage_stops_the_build_instead_of_crashing(ecosystem: Path) -> None:  # noqa: F811
    from wiki.build import build

    fake = FakeClient()
    _refuse_batches_after(fake, 1, _ApiError(LIMIT))  # notes go through, drafts are refused
    rep = build(["svc"], ecosystem, runner=Runner(ecosystem, client=fake, poll_s=0))
    assert "wiki drafts" in rep.stopped and "usage limit" in rep.stopped
    assert rep.repos["svc"].notes_written > 0  # what finished before the refusal is kept


def test_a_usage_limited_sync_request_stops_the_stage(tmp_path: Path) -> None:
    fake = FakeClient()
    fake.messages.stream = lambda **p: (_ for _ in ()).throw(_ApiError(LIMIT))
    with pytest.raises(BudgetExceeded, match="usage limit"):
        Runner(tmp_path, mode="sync", client=fake).run("t", [_req(1), _req(2)])


def test_any_other_400_still_raises(tmp_path: Path) -> None:
    fake = FakeClient()
    _refuse_batches_after(fake, 0, _ApiError("Error code: 400 - prompt is too long"))
    with pytest.raises(_ApiError):
        Runner(tmp_path, client=fake, poll_s=0).run("t", [_req(1)])
