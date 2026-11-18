from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def with_retries(
    func: Callable[[], T],
    retries: int = 3,
    base_delay_s: float = 1.0,
    retry_if: Callable[[Exception], bool] | None = None,
) -> T:
    """Call *func* up to *retries* times with exponential backoff.

    *retry_if* decides whether an error is worth another attempt; one it rejects (a 4xx, say)
    is raised at once instead of being retried into the same answer.
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == retries or (retry_if is not None and not retry_if(exc)):
                break
            time.sleep(base_delay_s * (2 ** (attempt - 1)))
    if last_error:
        raise last_error
    raise RuntimeError("with_retries reached invalid state")


class CircuitBreaker:
    """Stop calling a backend after *threshold* failures in a row.

    Once open, :meth:`allow` refuses calls until *cooldown_s* has passed, then lets one trial
    call through: a success closes the breaker, a failure reopens it for another cooldown.
    Without this, a dead or hung local model costs every remaining unit its full timeout and
    retries before the caller falls back.
    """

    def __init__(self, threshold: int = 5, cooldown_s: float = 600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = max(1, threshold)
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self.consecutive = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            if self._clock() - self.opened_at >= self.cooldown_s:
                self.opened_at = self._clock()  # one trial per cooldown, not a burst of them
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self.consecutive = 0
            self.opened_at = None

    def failure(self) -> bool:
        """Record a failure; True when this one opened (or reopened) the breaker."""
        with self._lock:
            self.consecutive += 1
            if self.consecutive >= self.threshold:
                self.opened_at = self._clock()
                return True
            return False


_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


def breaker_for(key: str, *, threshold: int = 5, cooldown_s: float = 600.0) -> CircuitBreaker:
    """The process-wide breaker for *key* (a backend endpoint), so clients built per repo share it."""
    with _BREAKERS_LOCK:
        b = _BREAKERS.get(key)
        if b is None:
            b = _BREAKERS[key] = CircuitBreaker(threshold, cooldown_s)
        return b
