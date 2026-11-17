"""Run a child process so a timeout kills everything it started, not just the child itself.

``subprocess.run(..., timeout=...)`` kills only the direct child: pytest's workers, a test's own
servers or pip's build backends keep running, holding CPU, ports and temp dirs. ``run`` starts the
child in a new session (its own process group) and, on timeout, kills the whole group before
re-raising ``TimeoutExpired``, so callers keep their existing ``except`` clauses."""
from __future__ import annotations

import os
import signal
import subprocess
from typing import Any


def run(cmd: list[str], *, timeout: float | None = None, check: bool = False,
        capture_output: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    with subprocess.Popen(cmd, start_new_session=True, **kwargs) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            out, err = proc.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
        except BaseException:
            kill_group(proc)
            raise
    result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check:
        result.check_returncode()
    return result


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
