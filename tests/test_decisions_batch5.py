"""decisions.close_idle loads, checks and writes a pack under pack_lock (batch 5)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from auditor import agent_pack
from auditor.agent_pack import AgentCodingPack, load_pack, write_pack


def _pack(out: Path, pid: str, status: str = "open") -> None:
    write_pack(AgentCodingPack(
        work_item_id=pid, kind="extraction", priority={"severity": "LOW", "score": 0.1},
        repo={"name": "r", "path": "", "base_branch": "dev"},
        location={"file_path": "a.py", "line": 1, "symbol": "", "code_snippet": "", "evidence": ""},
        narrative={"title": "t", "description": "d", "recommendation": "r", "acceptance_criteria": []},
        impact={}, verification={}, status=status), out)


def test_close_idle_holds_the_pack_lock_while_it_writes(tmp_path: Path, monkeypatch) -> None:
    from auditor.decisions import close_idle

    _pack(tmp_path, "pack-x")
    depths: list[int] = []
    real = agent_pack.write_pack

    def spy(pack, output_dir, **kw):
        depths.append(getattr(agent_pack._index_lock_depth, "n", 0))
        return real(pack, output_dir, **kw)

    monkeypatch.setattr(agent_pack, "write_pack", spy)
    assert close_idle(tmp_path, "pack-x", "obsolete", "gone") == "open"
    assert depths and depths[0] >= 1
    assert load_pack(tmp_path, "pack-x")["status"] == "obsolete"


def test_a_claim_landing_mid_close_is_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    """A claim that arrives after close_idle has read the pack waits for it, and is the status that stays."""
    from auditor.decisions import close_idle

    _pack(tmp_path, "pack-x")
    real_load = agent_pack.load_pack
    read = threading.Event()

    def slow_load(output_dir, pid):
        data = real_load(output_dir, pid)
        if threading.current_thread() is main:
            read.set()
            time.sleep(0.3)  # the claim tries to land here
        return data

    def claim() -> None:
        read.wait(5)
        with agent_pack.pack_lock(tmp_path):
            data = real_load(tmp_path, "pack-x")
            p = AgentCodingPack.from_dict(data, fallback_id="pack-x")
            p.apply_status("claimed", "")
            real_write(p, tmp_path, preserve_status=False)

    main = threading.current_thread()
    real_write = agent_pack.write_pack
    monkeypatch.setattr(agent_pack, "load_pack", slow_load)
    t = threading.Thread(target=claim)
    t.start()
    close_idle(tmp_path, "pack-x", "obsolete", "gone")
    t.join(5)
    assert real_load(tmp_path, "pack-x")["status"] == "claimed"
