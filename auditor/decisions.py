"""A person's approvals, kept as ``{"decisions": {id: decision}}`` in a file outside git, and the packs
they open and close. Shared by extraction candidates and module cycles."""
from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from auditor.fsutil import atomic_write_text

# Statuses an approval change may move a pack out of; anything else is someone's work in flight.
_IDLE = frozenset({"open", "obsolete", "wontfix"})


class DecisionConflict(Exception):
    """The pack is claimed or in progress, so its decision can't change under it."""


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_decisions(path: str | Path) -> dict[str, dict[str, Any]]:
    """``{id: decision}``; a missing file is none. A malformed one raises ValueError."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text) if text.strip() else {}
    found = data.get("decisions") if isinstance(data, dict) else None
    if found is None:
        return {}
    if not isinstance(found, dict):
        raise ValueError(f"{p}: 'decisions' must be an object keyed by id")  # noqa: TRY004 — bad data, not a bad call
    return {str(k): v for k, v in found.items() if isinstance(v, dict)}


def save_decisions(path: Path, decisions: dict[str, dict[str, Any]]) -> None:
    atomic_write_text(path, json.dumps({"decisions": decisions}, indent=2, sort_keys=True) + "\n")


def close_idle(output_dir: str | Path, pid: str, status: str, note: str) -> str | None:
    """Close an idle pack; raise DecisionConflict if someone holds it. Returns the status it had."""
    from auditor.agent_pack import AgentCodingPack, load_pack, write_pack

    data = load_pack(output_dir, pid)
    if data is None:
        return None
    pack = AgentCodingPack.from_dict(data, fallback_id=pid)
    current = pack.status or "open"
    if current not in _IDLE:
        raise DecisionConflict(f"{pid} is {current}: an agent is working on it. Finish or release it first.")
    if current != status:
        pack.apply_status(status, note)
        write_pack(pack, output_dir, preserve_status=False)
    return current
