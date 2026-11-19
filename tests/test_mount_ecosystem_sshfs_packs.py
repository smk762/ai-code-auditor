"""Regression tests for audit-pack fixes in scripts/mount_ecosystem_sshfs.py (no real mounts: subprocess is faked)."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("mount_ecosystem_sshfs", ROOT / "scripts" / "mount_ecosystem_sshfs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def config(tmp_path: Path) -> Path:
    c = tmp_path / "sshfs_mounts.yaml"
    c.write_text(f"mounts:\n  - remote: host:/srv/a\n    local: {tmp_path / 'a'}\n")
    return c


def test_a_disconnected_mount_still_counts_as_a_mountpoint(mod, monkeypatch) -> None:
    def run(cmd, **kw):
        if cmd[0] == "mountpoint":  # stat() fails with ENOTCONN on a dead sshfs mount
            return SimpleNamespace(returncode=32, stdout="", stderr="Transport endpoint is not connected")
        return SimpleNamespace(returncode=0, stdout="/mnt/a", stderr="")  # findmnt reads the mount table

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._is_exact_mountpoint("/mnt/a")


def test_umount_reports_failure(mod, monkeypatch, config: Path) -> None:
    monkeypatch.setattr(mod, "_find_executable", lambda *a, **k: "/usr/bin/fusermount3")
    monkeypatch.setattr(mod, "_is_sshfs_mount", lambda local: True)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="Device or resource busy"))
    assert mod.cmd_umount(config, quiet=True) == 1
    assert mod.cmd_umount(config) == 1


def test_mount_does_not_wait_on_a_pipe_the_ssh_child_keeps_open(mod, monkeypatch, config: Path) -> None:
    monkeypatch.setattr(mod, "_find_executable", lambda *a, **k: "/usr/bin/" + a[0])
    monkeypatch.setattr(mod, "_ensure_mountpoint_dir", lambda local, fm: (True, None))
    monkeypatch.setattr(mod, "_is_exact_mountpoint", lambda local: False)

    def run(cmd, **kw):
        if kw.get("capture_output") or kw.get("stderr") is subprocess.PIPE:
            raise subprocess.TimeoutExpired(cmd, 120)  # the daemon's ssh child holds the pipe open
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.cmd_mount(config) == 0


def test_mount_timeout_with_the_mount_up_is_not_a_failure(mod, monkeypatch, config: Path) -> None:
    monkeypatch.setattr(mod, "_find_executable", lambda *a, **k: "/usr/bin/" + a[0])
    monkeypatch.setattr(mod, "_ensure_mountpoint_dir", lambda local, fm: (True, None))
    checks = iter([False, True])  # not mounted before; mounted when the timeout is re-checked
    monkeypatch.setattr(mod, "_is_exact_mountpoint", lambda local: next(checks))

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.cmd_mount(config) == 0


def test_a_hung_findmnt_reads_as_a_mountpoint_not_a_crash(mod, monkeypatch) -> None:
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod._is_exact_mountpoint("/mnt/a") is True
    assert mod._fuse_sshfs_fstype("/mnt/a") is None


def test_a_hung_unmount_is_lazily_detached_and_teardown_continues(mod, monkeypatch, tmp_path: Path) -> None:
    c = tmp_path / "sshfs_mounts.yaml"
    c.write_text(f"mounts:\n  - remote: host:/srv/a\n    local: {tmp_path / 'a'}\n"
                 f"  - remote: host:/srv/b\n    local: {tmp_path / 'b'}\n")
    monkeypatch.setattr(mod, "_find_executable", lambda *a, **k: "/usr/bin/fusermount3")
    monkeypatch.setattr(mod, "_is_sshfs_mount", lambda local: True)
    calls = []

    def run(cmd, **kw):
        calls.append((cmd[1], Path(cmd[2]).name))
        if cmd[1] == "-u" and cmd[2].endswith("/b"):
            raise subprocess.TimeoutExpired(cmd, 60)  # b's peer is gone
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.cmd_umount(c, quiet=True) == 0
    assert calls == [("-u", "b"), ("-uz", "b"), ("-u", "a")]  # reversed teardown reaches a


def test_teardown_reports_a_mount_that_will_not_detach(mod, monkeypatch, config: Path) -> None:
    monkeypatch.setattr(mod, "_find_executable", lambda *a, **k: "/usr/bin/fusermount3")
    monkeypatch.setattr(mod, "_is_sshfs_mount", lambda local: True)

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.cmd_umount(config) == 1
