"""Regression tests for auditor/validate_cache.py audit packs (batch 4)."""
from __future__ import annotations

import fcntl
from pathlib import Path

from auditor import validate_cache


def test_a_waiter_on_the_lock_still_excludes_a_later_evictor(tmp_path: Path) -> None:  # 6bd18895ec
    entry = tmp_path / "venv-1"
    entry.mkdir()
    lock_path = tmp_path / ".venv-1.lock"
    lock_path.touch()
    with open(lock_path, "a") as builder:  # opened while the first eviction runs, before it locks
        assert validate_cache._remove(tmp_path, entry)
        fcntl.flock(builder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the builder now holds the lock ...
        entry.mkdir()  # ... and rebuilds the entry
        assert not validate_cache._remove(tmp_path, entry)  # so a second eviction must not get it
        assert entry.is_dir()
