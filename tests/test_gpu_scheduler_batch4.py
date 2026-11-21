"""gpu_scheduler: the gothmog transient-error limit counts consecutive failures only."""
from __future__ import annotations

import pytest
import requests

from auditor import gpu_scheduler
from auditor.gpu_scheduler import GpuScheduler


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def sched(tmp_path, monkeypatch):
    monkeypatch.setenv("GOTHMOG_URL", "http://gothmog.test")
    monkeypatch.setenv("GOTHMOG_API_KEY", "")
    monkeypatch.delenv("AI_AUDIT_SKIP_GPU_SCHEDULER", raising=False)
    monkeypatch.setattr(gpu_scheduler.time, "sleep", lambda s: None)
    return GpuScheduler(config_path=tmp_path / "absent.yaml")


def test_scattered_transient_errors_between_503s_do_not_abort_the_wait(sched, monkeypatch):
    sched.gothmog_max_transient_retries = 2
    # Three blips in total, never more than one in a row, each followed by a healthy 503.
    script = ["conn", 503, "conn", 503, 502, 503, "grant"]
    sleeps: list[float] = []
    monkeypatch.setattr(gpu_scheduler.time, "sleep", sleeps.append)

    def post(*a, **k):
        step = script.pop(0)
        if step == "conn":
            raise requests.exceptions.ConnectionError("blip")
        if step == "grant":
            return _Resp(200, {"token": "tok", "vram_free_mb": 9000})
        return _Resp(step)

    monkeypatch.setattr(gpu_scheduler.requests, "post", post)
    cap = sched.wait_for_capacity()
    assert cap.offload_mode == "gothmog_managed" and not script
    # The backoff restarts from gothmog_backoff_s after each healthy answer.
    backoffs = [s for s in sleeps if s != sched.retry_interval_s]
    assert backoffs == [min(float(sched.retry_interval_s), sched.gothmog_backoff_s)] * 3
