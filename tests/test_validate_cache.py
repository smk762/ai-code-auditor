import fcntl
import os
import time
from pathlib import Path

from auditor import validate_cache

DAY = 86400


def _entry(cache: Path, name: str, *, used_days_ago: float, size: int = 1000, now: float) -> Path:
    e = cache / name
    (e / "lib").mkdir(parents=True)
    (e / "lib" / "blob").write_bytes(b"x" * size)
    (e / ".ready").write_text("k")
    t = now - used_days_ago * DAY
    os.utime(e / ".ready", (t, t))
    (cache / f".{name}.lock").touch()
    (cache / f".{name}.txt").write_text("reqs")
    return e


def test_old_entries_go_and_recent_ones_stay(tmp_path: Path) -> None:
    now = time.time()
    old = _entry(tmp_path, "old-1", used_days_ago=10, now=now)
    new = _entry(tmp_path, "new-1", used_days_ago=1, now=now)
    r = validate_cache.evict(tmp_path, now=now, force=True, age_days=7, limit_bytes=10 ** 9)
    assert r["removed"] == ["old-1"] and not old.exists() and new.exists()
    assert not (tmp_path / ".old-1.txt").exists()
    assert (tmp_path / ".old-1.lock").exists()  # never unlinked: a waiter may hold it open


def test_over_the_size_cap_least_recently_used_go_first(tmp_path: Path) -> None:
    now = time.time()
    for name, days in (("a", 3), ("b", 2), ("c", 1)):
        _entry(tmp_path, name, used_days_ago=days, size=1000, now=now)
    r = validate_cache.evict(tmp_path, now=now, force=True, age_days=30, limit_bytes=2500)
    assert r["removed"] == ["a"] and r["kept_bytes"] <= 2500


def test_an_entry_in_use_or_used_this_hour_is_never_removed(tmp_path: Path) -> None:
    now = time.time()
    _entry(tmp_path, "hot", used_days_ago=0.01, now=now)  # used 15 minutes ago
    locked = _entry(tmp_path, "locked", used_days_ago=30, now=now)
    with open(tmp_path / ".locked.lock", "a") as fh:  # another request is building or using it
        fcntl.flock(fh, fcntl.LOCK_EX)
        r = validate_cache.evict(tmp_path, now=now, force=True, age_days=1, limit_bytes=0)
    assert r["removed"] == [] and locked.exists() and (tmp_path / "hot").exists()


def test_runs_at_most_once_an_hour(tmp_path: Path) -> None:
    now = time.time()
    _entry(tmp_path, "old", used_days_ago=30, now=now)
    (tmp_path / validate_cache.STAMP).touch()
    assert validate_cache.evict(tmp_path, now=now, age_days=7)["removed"] == []
    assert validate_cache.evict(tmp_path, now=now + 3700, age_days=7)["removed"] == ["old"]


def test_an_interrupted_removal_never_leaves_a_ready_entry(tmp_path: Path, monkeypatch) -> None:
    """The removal runs on a daemon thread, which dies with its process: whatever it leaves behind
    must read as an unfinished build (rebuilt on next use), never as a ready env with packages gone."""
    now = time.time()
    e = _entry(tmp_path, "repo-old", used_days_ago=30, now=now)

    def killed_part_way(path, ignore_errors=False):
        (Path(path) / "lib" / "blob").unlink()  # some of the env is gone, then the thread dies

    monkeypatch.setattr(validate_cache.shutil, "rmtree", killed_part_way)
    r = validate_cache.evict(tmp_path, now=now, force=True, age_days=7, limit_bytes=10 ** 9)

    assert r["removed"] == []  # not counted as removed: the entry is still there
    assert e.exists() and not (e / ".ready").exists()
