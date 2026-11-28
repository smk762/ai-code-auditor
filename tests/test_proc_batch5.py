"""Regression tests for auditor/proc.py audit packs (batch 5)."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from auditor import proc

# The child starts a daemon in its own session, which keeps the inherited stdout/stderr open
# after the child's group is killed, then sleeps past the timeout itself.
_DAEMON = ("import subprocess, sys, time; "
           "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True); "
           "print('started', flush=True); time.sleep(20)")


def test_a_daemon_holding_the_pipes_cannot_outlast_the_timeout(monkeypatch) -> None:  # 276f869d95
    monkeypatch.setattr(proc, "_DRAIN_TIMEOUT_S", 0.5, raising=False)
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run([sys.executable, "-c", _DAEMON], timeout=1, capture_output=True, text=True)
    assert time.monotonic() - t0 < 10
