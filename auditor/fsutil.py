"""Files and paths: atomic replacement (readers see the old file or the new one, never half of it), and "~" in config."""
from __future__ import annotations

import os
import threading
from pathlib import Path


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """Replace *path* with *text*, durably: the data is fsynced before the rename and the
    directory after it, so a crash leaves the old content or the new, never an empty file."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        _fsync_path(tmp)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_path(path: Path) -> None:
    """Flush *path*'s data to disk (fsync works on a read-only descriptor)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(directory: Path) -> None:
    """Flush the rename's directory entry; best-effort where directories can't be opened."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def host_path(path: str) -> str:
    """A configured path with a leading ``~`` expanded to the host user's home.

    In Docker the repos are mounted at their host paths while ``HOME`` is ``/``, so compose passes
    the host's home as ``AI_AUDIT_HOST_HOME``. Without a usable home it raises: ``/mirrors`` would
    read as every repo being empty."""
    p = str(path or "")
    if p != "~" and not p.startswith("~/"):
        return p
    home = (os.getenv("AI_AUDIT_HOST_HOME", "").strip() or os.path.expanduser("~")).rstrip("/")
    if not home.startswith("/"):
        raise ValueError(f"can't expand {p!r}: set AI_AUDIT_HOST_HOME to the host user's home directory")
    return home + p[1:]
