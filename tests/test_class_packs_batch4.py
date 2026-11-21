"""record_guard stores the values it validated, normalised (6ff32ef57b)."""
from __future__ import annotations

import json
from pathlib import Path

from auditor import class_packs as cp
from auditor.agent_pack import load_pack, packs_dir


def _child(out: Path, pid: str, repo: str) -> None:
    d = packs_dir(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.yaml").write_text(json.dumps({
        "work_item_id": pid, "kind": "nightly", "status": "open", "priority": {"severity": "HIGH", "score": 0.7},
        "repo": {"name": repo}, "location": {"file_path": "app/a.py", "line": 10},
        "narrative": {"title": f"{pid} lets the caller pick the URL"},
        "governance": {"finding_class": {"id": "ssrf", "guard": "helper"}}}))


def test_record_guard_stores_normalised_repo_commit_and_paths(tmp_path: Path) -> None:
    _child(tmp_path, "a", "r1")
    _child(tmp_path, "b", "r2")
    cp.emit(tmp_path)
    sha = "ABCDEF1000000000000000000000000000000000"
    cp.record_guard(tmp_path, "pack-class-ssrf", repo=" r1 ", commit=f" {sha}\n", proof_test=" tests/test_g.py ",
                    fixtures=[" tests/fx.py"])
    guards = (load_pack(tmp_path, "pack-class-ssrf") or {})["governance"]["class_guard"]
    assert list(guards) == ["r1"]
    g = guards["r1"]
    assert g["commit"] == sha.lower()
    assert g["proof_test"] == "tests/test_g.py" and g["fixtures"] == ["tests/fx.py"]
