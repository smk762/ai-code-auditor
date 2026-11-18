from __future__ import annotations

import pytest

import auditor.retry as retry_mod
from auditor.retry import CircuitBreaker, breaker_for, with_retries


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: None)


def test_retry_if_stops_on_an_error_it_rejects() -> None:
    calls = []

    def fail() -> None:
        calls.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        with_retries(fail, retries=3, retry_if=lambda e: False)
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(ValueError):
        with_retries(fail, retries=3)  # no predicate: every error is retried, as before
    assert len(calls) == 3


def test_breaker_opens_at_threshold_and_lets_one_trial_through_after_cooldown() -> None:
    now = [0.0]
    b = CircuitBreaker(threshold=3, cooldown_s=60, clock=lambda: now[0])
    assert [b.failure() for _ in range(3)] == [False, False, True]
    assert not b.allow()
    now[0] = 61
    assert b.allow() and not b.allow()  # one trial, not a burst
    assert b.failure()  # the trial failed: open for another cooldown
    now[0] = 100
    assert not b.allow()
    now[0] = 122
    assert b.allow()
    b.success()
    assert not b.is_open and b.allow() and b.consecutive == 0


def test_a_success_resets_the_run_of_failures() -> None:
    b = CircuitBreaker(threshold=2)
    b.failure()
    b.success()
    assert not b.failure()


def test_breakers_are_shared_per_key() -> None:
    assert breaker_for("ollama:a") is breaker_for("ollama:a")
    assert breaker_for("ollama:a") is not breaker_for("ollama:b")
