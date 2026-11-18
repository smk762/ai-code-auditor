"""Regression tests for auditor/gpu_scheduler.py (gothmog token handling, VRAM probe, retries)."""
from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest
import requests

from auditor import gpu_scheduler
from auditor.gpu_scheduler import GpuScheduler, get_vram_info


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


def _granting(*tokens: str):
    it = iter(tokens)
    return lambda *a, **k: _Resp(200, {"token": next(it), "vram_free_mb": 9000})


def _recording_delete(deleted: list[str], status: int = 204):
    def delete(url, **k):
        deleted.append(url.rsplit("/", 1)[-1])
        return _Resp(status)
    return delete


def test_second_acquire_does_not_leak_the_first_token(sched, monkeypatch):
    monkeypatch.setattr(gpu_scheduler.requests, "post", _granting("tok-a", "tok-b"))
    deleted: list[str] = []
    monkeypatch.setattr(gpu_scheduler.requests, "delete", _recording_delete(deleted))
    sched.wait_for_capacity()
    sched.wait_for_capacity()
    sched.release_capacity()
    assert sorted(deleted) == ["tok-a", "tok-b"]


def test_release_with_token_frees_only_that_grant(sched, monkeypatch):
    monkeypatch.setattr(gpu_scheduler.requests, "post", _granting("tok-a", "tok-b"))
    deleted: list[str] = []
    monkeypatch.setattr(gpu_scheduler.requests, "delete", _recording_delete(deleted))
    first = sched.wait_for_capacity()
    second = sched.wait_for_capacity()
    assert (first.token, second.token) == ("tok-a", "tok-b")
    sched.release_capacity(first.token)
    assert deleted == ["tok-a"]
    sched.release_capacity(second.token)
    assert deleted == ["tok-a", "tok-b"]


def test_rejected_release_is_logged_as_a_warning(sched, monkeypatch, caplog):
    monkeypatch.setattr(gpu_scheduler.requests, "post", _granting("tok-a"))
    monkeypatch.setattr(gpu_scheduler.requests, "delete", _recording_delete([], status=500))
    sched.wait_for_capacity()
    with caplog.at_level(logging.INFO, logger="auditor.gpu_scheduler"):
        sched.release_capacity()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("500" in r.getMessage() for r in warnings)
    assert not any("released." in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("failure", ["conn", "timeout", 502, 504])
def test_transient_gothmog_errors_are_retried(sched, monkeypatch, failure):
    calls = {"n": 0}

    def post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            if failure == "conn":
                raise requests.exceptions.ConnectionError("reset")
            if failure == "timeout":
                raise requests.exceptions.ReadTimeout("slow")
            return _Resp(failure)
        return _Resp(200, {"token": "tok-a", "vram_free_mb": 9000})

    monkeypatch.setattr(gpu_scheduler.requests, "post", post)
    cap = sched.wait_for_capacity()
    assert cap.offload_mode == "gothmog_managed"
    assert calls["n"] == 2


def test_transient_retries_are_bounded(sched, monkeypatch):
    sched.gothmog_max_transient_retries = 2
    calls = {"n": 0}

    def post(*a, **k):
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(gpu_scheduler.requests, "post", post)
    with pytest.raises(requests.exceptions.ConnectionError):
        sched.wait_for_capacity()
    assert calls["n"] == 3


@pytest.mark.parametrize(
    "outcome",
    [
        SimpleNamespace(returncode=0, stdout="", stderr=""),
        PermissionError("not executable"),
        subprocess.SubprocessError("boom"),
    ],
)
def test_vram_probe_returns_none_on_failure(monkeypatch, outcome):
    def run(*a, **k):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(gpu_scheduler.subprocess, "run", run)
    assert get_vram_info() is None
