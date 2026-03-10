from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def with_retries(func: Callable[[], T], retries: int = 3, base_delay_s: float = 1.0) -> T:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == retries:
                break
            time.sleep(base_delay_s * (2 ** (attempt - 1)))
    if last_error:
        raise last_error
    raise RuntimeError("with_retries reached invalid state")
