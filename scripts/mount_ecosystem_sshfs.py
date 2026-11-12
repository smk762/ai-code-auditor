#!/usr/bin/env python3
"""
Start or stop all ecosystem SSHFS mounts defined in config/sshfs_mounts.yaml.

Designed for a single systemd --user unit: one switch for the whole audit ecosystem.

Mount detection uses *exact mountpoints* (not findmnt -T, which matches parent fs on
empty dirs). Stop only runs fusermount on fuse.sshfs mounts so we do not fight
other mount types.
"""
from __future__ import annotations

import argparse
import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

# systemd --user often has a minimal PATH; interactive shells find sshfs in /usr/local/bin.
_STANDARD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _find_executable(name: str, *, env_override: str | None = None) -> str | None:
    if env_override:
        override = os.environ.get(env_override, "").strip()
        if override and Path(override).is_file() and os.access(override, os.X_OK):
            return override
    combined = f"{_STANDARD_PATH}{os.pathsep}{os.environ.get('PATH', '')}"
    found = shutil.which(name, path=combined)
    if found:
        return found
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_config(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mounts = raw.get("mounts") or []
    if not isinstance(mounts, list):
        raise ValueError("sshfs_mounts.yaml: 'mounts' must be a list")
    opts = str(raw.get("sshfs_options") or "").strip()
    opts = os.path.expandvars(opts)
    return {"sshfs_options": opts, "mounts": mounts}


def _sshfs_cmd(sshfs_bin: str, remote: str, local: str, options: str) -> list[str]:
    cmd = [sshfs_bin, remote, local]
    if options:
        cmd.extend(["-o", options])
    return cmd


def _is_exact_mountpoint(local: str) -> bool:
    """True only if `local` is itself a mount point (not just a dir under /)."""
    try:
        r = subprocess.run(
            ["mountpoint", "-q", local],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except FileNotFoundError:
        # util-linux mountpoint missing: fall back to findmnt --mountpoint
        try:
            r = subprocess.run(
                ["findmnt", "-M", local],
                capture_output=True,
                timeout=10,
            )
            return r.returncode == 0
        except FileNotFoundError:
            return False


def _fuse_sshfs_fstype(local: str) -> str | None:
    """Return FSTYPE if `local` is a mountpoint; else None."""
    if not _is_exact_mountpoint(local):
        return None
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "-M", local],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            fs = (r.stdout or "").strip()
            return fs or None
        # util-linux without -M: best-effort (path must be exact mountpoint)
        r2 = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", local],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r2.returncode != 0:
            return None
        fs = (r2.stdout or "").strip()
        return fs or None
    except FileNotFoundError:
        return None


def _is_sshfs_mount(local: str) -> bool:
    fs = _fuse_sshfs_fstype(local)
    if not fs:
        return False
    return fs.lower() in ("fuse.sshfs", "fuse.ssh")


def _clear_stale_fuse_mountpoint(local: str, fusermount: str | None) -> None:
    """Dead sshfs leaves a directory that stat() fails with ENOTCONN; unmount it."""
    try:
        os.stat(local)
        return
    except FileNotFoundError:
        return
    except OSError as e:
        if e.errno != errno.ENOTCONN:
            return

    print(
        f"clearing stale FUSE mountpoint (errno {errno.ENOTCONN} transport not connected): {local}",
        file=sys.stderr,
    )
    if fusermount:
        subprocess.run([fusermount, "-u", local], capture_output=True, text=True, timeout=60)
        subprocess.run([fusermount, "-uz", local], capture_output=True, text=True, timeout=60)
    umount = _find_executable("umount")
    if umount:
        subprocess.run([umount, "-l", local], capture_output=True, text=True, timeout=60)


def _ensure_mountpoint_dir(local: str, fusermount: str | None) -> tuple[bool, str | None]:
    """Create local dir; recover from stale FUSE first. Avoid pathlib.mkdir on broken FUSE."""
    _clear_stale_fuse_mountpoint(local, fusermount)
    try:
        os.makedirs(local, mode=0o755, exist_ok=True)
    except OSError as e:
        return False, str(e)
    try:
        os.stat(local)
    except OSError as e:
        if e.errno == errno.ENOTCONN:
            return False, f"still broken after unmount attempts: {local} ({e})"
        return False, str(e)
    return True, None


def cmd_mount(config_path: Path) -> int:
    sshfs_bin = _find_executable("sshfs", env_override="SSHFS_BIN")
    if not sshfs_bin:
        print(
            "sshfs not found in PATH. Install it (e.g. sudo apt install sshfs) or set SSHFS_BIN "
            "to the full path. Under systemd --user, ensure PATH includes /usr/bin and "
            "/usr/local/bin (see contrib systemd unit Environment=PATH).",
            file=sys.stderr,
        )
        return 1

    fusermount = _find_executable("fusermount3", env_override="FUSERMOUNT3_BIN") or _find_executable(
        "fusermount", env_override="FUSERMOUNT_BIN"
    )

    cfg = _load_config(config_path)
    options = cfg["sshfs_options"]
    mounts = cfg["mounts"]
    started = 0
    failures = 0

    for m in mounts:
        if not isinstance(m, dict):
            continue
        remote = str(m.get("remote", "")).strip()
        local = os.path.expandvars(str(m.get("local", "")).strip())
        if not remote or not local:
            continue
        ok, prep_err = _ensure_mountpoint_dir(local, fusermount)
        if not ok:
            print(f"cannot prepare mountpoint {local}: {prep_err}", file=sys.stderr)
            failures += 1
            continue
        if _is_exact_mountpoint(local):
            print(f"skip (already a mountpoint): {local}", file=sys.stderr)
            continue
        cmd = _sshfs_cmd(sshfs_bin, remote, local, options)
        print(" ".join(cmd), file=sys.stderr)
        # Capture output so one bad remote does not abort the rest of the list.
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"sshfs timed out after 120s: {local} <- {remote}", file=sys.stderr)
            failures += 1
            continue
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()
            msg = f"sshfs failed ({r.returncode}): {local} <- {remote}"
            if detail:
                msg = f"{msg}\n{detail}"
            print(msg, file=sys.stderr)
            failures += 1
            continue
        started += 1

    if started == 0 and failures == 0:
        print("No new sshfs mounts needed (empty config or all paths already mountpoints).", file=sys.stderr)
    elif started:
        print(f"Started {started} sshfs mount(s).", file=sys.stderr)
    if failures:
        print(f"sshfs: {failures} mount(s) failed (others were still attempted).", file=sys.stderr)
        return 1
    return 0


def cmd_umount(config_path: Path, quiet: bool = False) -> int:
    fusermount = _find_executable("fusermount3", env_override="FUSERMOUNT3_BIN") or _find_executable(
        "fusermount", env_override="FUSERMOUNT_BIN"
    )
    if not fusermount:
        print(
            "fusermount3/fusermount not found. Install fuse3 (e.g. sudo apt install fuse3 sshfs) "
            "or set FUSERMOUNT3_BIN to the full path.",
            file=sys.stderr,
        )
        return 1

    cfg = _load_config(config_path)
    mounts = list(reversed(cfg["mounts"]))  # reverse order for teardown
    for m in mounts:
        if not isinstance(m, dict):
            continue
        local = os.path.expandvars(str(m.get("local", "")).strip())
        if not local:
            continue
        if not _is_sshfs_mount(local):
            if not quiet and _is_exact_mountpoint(local):
                fs = _fuse_sshfs_fstype(local) or "?"
                print(f"skip umount (not fuse.sshfs, is {fs}): {local}", file=sys.stderr)
            continue
        cmd = [fusermount, "-u", local]
        if not quiet:
            print(" ".join(cmd), file=sys.stderr)
        r = subprocess.run(cmd, capture_output=quiet, text=True, timeout=60)
        if r.returncode != 0 and not quiet:
            print(r.stderr or r.stdout or f"umount failed: {local}", file=sys.stderr)
    return 0


def cmd_status(config_path: Path) -> int:
    cfg = _load_config(config_path)
    for m in cfg["mounts"]:
        if not isinstance(m, dict):
            continue
        local = os.path.expandvars(str(m.get("local", "")).strip())
        remote = str(m.get("remote", "")).strip()
        if not local:
            continue
        if not _is_exact_mountpoint(local):
            st = "not mounted"
        else:
            fs = _fuse_sshfs_fstype(local) or "unknown"
            st = f"mounted ({fs})"
        print(f"{st}\t{local}\t{remote}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mount or unmount ecosystem SSHFS shares.")
    parser.add_argument(
        "action",
        choices=("start", "stop", "status"),
        help="start: sshfs each path that is not yet a mountpoint (continues on per-mount failure); stop: fusermount fuse.sshfs only",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_repo_root() / "config" / "sshfs_mounts.yaml",
        help="Path to sshfs_mounts.yaml",
    )
    args = parser.parse_args()
    config_path = args.config.resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        return 1

    if args.action == "start":
        return cmd_mount(config_path)
    if args.action == "stop":
        return cmd_umount(config_path)
    return cmd_status(config_path)


if __name__ == "__main__":
    raise SystemExit(main())
