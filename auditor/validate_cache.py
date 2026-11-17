"""Eviction for validate_patch's cached test environments (Python venvs, npm node_modules).

Each entry is ``<cache_dir>/<name>-<hash>/`` with a ``.ready`` marker once built, a lock file
``<cache_dir>/.<entry>.lock`` held while it is built or used, and sometimes ``.<entry>.txt``.
``.ready``'s mtime is the entry's last use (``touch`` on every reuse). After a build,
``evict`` removes entries unused for ``max_age_days``, then the least recently used until the
cache is under ``max_bytes``. It never removes an entry used in the last hour or one whose lock
another process holds, and it runs at most once an hour per cache."""
from __future__ import annotations

import fcntl
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_IDLE_S = 3600
STAMP = ".last-eviction"


def max_age_days() -> float:
    return _env_float("AI_AUDIT_VALIDATE_CACHE_MAX_AGE_DAYS", 7.0)


def max_bytes() -> int:
    return int(_env_float("AI_AUDIT_VALIDATE_CACHE_MAX_GB", 8.0) * 1024 ** 3)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def touch(entry: Path) -> None:
    """Record a use of *entry* (its ``.ready`` mtime is the last-used time)."""
    try:
        os.utime(entry / ".ready")
    except OSError:
        pass


def _size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _last_used(entry: Path) -> float:
    for p in (entry / ".ready", entry):  # an entry without .ready is a failed or partial build
        try:
            return p.stat().st_mtime
        except OSError:
            continue
    return 0.0


def _remove(cache_dir: Path, entry: Path) -> bool:
    """Delete *entry* if nobody holds its lock. True when removed."""
    lock_path = cache_dir / f".{entry.name}.lock"
    try:
        with open(lock_path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False  # being built or used right now
            shutil.rmtree(entry, ignore_errors=True)
            (cache_dir / f".{entry.name}.txt").unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)
    except OSError:
        return False
    return not entry.exists()


def evict(cache_dir: Path, *, now: float | None = None, force: bool = False,
          age_days: float | None = None, limit_bytes: int | None = None) -> dict:
    """Trim *cache_dir*; returns ``{"removed": [...], "freed_bytes": n, "kept_bytes": n}``."""
    now = time.time() if now is None else now
    cache_dir = Path(cache_dir)
    stamp = cache_dir / STAMP
    out = {"removed": [], "freed_bytes": 0, "kept_bytes": 0}
    if not cache_dir.is_dir():
        return out
    if not force:
        try:
            if now - stamp.stat().st_mtime < MIN_IDLE_S:
                return out
        except OSError:
            pass
    stamp.touch()
    age_s = (max_age_days() if age_days is None else age_days) * 86400
    limit = max_bytes() if limit_bytes is None else limit_bytes
    entries = sorted(((_last_used(e), e) for e in cache_dir.iterdir() if e.is_dir() and not e.name.startswith(".")),
                     key=lambda x: x[0])  # least recently used first
    sizes = {e: _size(e) for _, e in entries}
    total = sum(sizes.values())
    for used, entry in entries:
        idle = now - used
        if idle < MIN_IDLE_S:
            continue
        if (idle > age_s or total > limit) and _remove(cache_dir, entry):
            out["removed"].append(entry.name)
            out["freed_bytes"] += sizes[entry]
            total -= sizes[entry]
    out["kept_bytes"] = total
    if out["removed"]:
        logger.info("validate cache %s: removed %d entries, freed %.1f GB, %.1f GB left", cache_dir,
                    len(out["removed"]), out["freed_bytes"] / 1024 ** 3, total / 1024 ** 3)
    return out


def evict_in_background(cache_dir: Path) -> None:
    """Run :func:`evict` off the request: walking gigabytes of envs takes seconds."""
    import threading

    def _run() -> None:
        try:
            evict(cache_dir)
        except Exception:  # noqa: BLE001 — eviction is housekeeping; the next build retries it
            logger.exception("validate cache eviction failed for %s", cache_dir)

    threading.Thread(target=_run, name="validate-cache-evict", daemon=True).start()
