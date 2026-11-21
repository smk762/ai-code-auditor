"""atomic_write_text is durable, not only atomic (pack a22b3ee7f4)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from auditor import fsutil


def test_data_is_fsynced_before_the_rename_and_the_dir_after(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
        real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "fsync", fsync)
    monkeypatch.setattr(fsutil.os, "replace", replace)
    target = tmp_path / "state.json"
    target.write_text("old")
    fsutil.atomic_write_text(target, '{"new": true}\n')
    assert target.read_text() == '{"new": true}\n'
    assert events == ["fsync-file", "replace", "fsync-dir"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
