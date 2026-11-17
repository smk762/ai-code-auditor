import subprocess
import time
from pathlib import Path

import pytest

from auditor.proc import run


def _alive(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status")
    try:
        return "\nState:\tZ" not in status.read_text()  # a zombie is dead, just not reaped
    except (FileNotFoundError, ProcessLookupError):  # gone between the lookup and the read
        return False


def test_timeout_kills_the_whole_process_group() -> None:
    # The child starts a grandchild (as pytest workers or a test's server would) and waits.
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run(["sh", "-c", "sleep 30 & echo $!; wait"], timeout=1, capture_output=True, text=True)
    grandchild = int(exc.value.output.strip())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)


def test_behaves_like_subprocess_run() -> None:
    r = run(["sh", "-c", "echo out; echo err >&2; exit 3"], capture_output=True, text=True)
    assert (r.returncode, r.stdout, r.stderr) == (3, "out\n", "err\n")
    with pytest.raises(subprocess.CalledProcessError):
        run(["false"], check=True)
