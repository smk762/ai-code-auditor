"""class_packs: prefix rule selectors, unparseable sources, gone children and malformed guard
records (batch 5 packs b0d2a775a6, 30beb1abf4, 62d1221306, 0c7de8d6a0)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from auditor import class_packs as cp
from auditor.agent_pack import load_pack, packs_dir

SHA = "a" * 40
ZIP_SRC = "def pairs(raw_paths, names):\n    return list(zip(raw_paths, names))\n"


def _child(out: Path, pid: str, repo: str, status: str = "open") -> None:
    d = packs_dir(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.yaml").write_text(json.dumps({
        "work_item_id": pid, "kind": "nightly", "status": status, "priority": {"severity": "HIGH", "score": 0.7},
        "repo": {"name": repo}, "location": {"file_path": "app/a.py", "line": 10},
        "narrative": {"title": f"{pid} lets the caller pick the URL"},
        "governance": {"finding_class": {"id": "ssrf", "guard": "helper"}}}))


def _class_pack(out: Path, guard: dict, extra_children: list[str] = ()) -> str:
    """A class pack over children a (r1) and b (r2) with *guard* stored for r1, written as is."""
    _child(out, "a", "r1")
    _child(out, "b", "r2")
    cp.emit(out)
    pid = "pack-class-ssrf"
    f = packs_dir(out) / f"{pid}.yaml"
    data = load_pack(out, pid)
    data["governance"]["class_guard"] = {"r1": guard}
    data["governance"]["children"] = [*data["governance"]["children"], *extra_children]
    f.write_text(json.dumps(data))
    return pid


def _git_mirror(tmp_path: Path) -> Path:
    m = tmp_path / "mirror"
    m.mkdir()
    for args in (("init", "-q", "-b", "dev"), ("config", "user.email", "t@t"), ("config", "user.name", "t"),
                 ("commit", "-q", "--allow-empty", "-m", "base")):
        subprocess.run(["git", *args], cwd=m, check=True, capture_output=True)
    return m


@pytest.fixture
def no_scans(monkeypatch):
    monkeypatch.setattr(cp, "_scans_by_repo", lambda *_: {})


def test_a_prefix_selector_keeps_the_hits_it_selects() -> None:
    hits = cp.rule_hits(ZIP_SRC, "m.py", {"tool": "ruff", "select": ["B9"]})
    assert [(h.line, h.code) for h in hits] == [(2, "B905")]


def test_an_unparseable_source_is_never_no_hits() -> None:
    assert cp.rule_hits("def f(:\n    return zip(a, b)\n", "m.py", {"tool": "ruff", "select": ["B905"]}) is None


def test_a_gone_child_pack_does_not_block_resolution(tmp_path: Path, monkeypatch, no_scans) -> None:
    out = tmp_path / "reports"
    pid = _class_pack(out, {"commit": SHA, "proof_test": "tests/t.py", "proof": {"status": "proven"}},
                      extra_children=["ghost"])
    for c in ("a", "b"):
        _child(out, c, "r1" if c == "a" else "r2", status="resolved")
    monkeypatch.setattr("auditor.pack_gc.mirror_paths", lambda *_: {})
    cp.run(out, runner=lambda *a: pytest.fail("a proven guard is not re-proven"))
    assert load_pack(out, pid)["status"] == "resolved"


def test_an_unreadable_child_pack_still_blocks_resolution(tmp_path: Path, monkeypatch, no_scans) -> None:
    out = tmp_path / "reports"
    pid = _class_pack(out, {"commit": SHA, "proof_test": "tests/t.py", "proof": {"status": "proven"}},
                      extra_children=["broken"])
    for c in ("a", "b"):
        _child(out, c, "r1" if c == "a" else "r2", status="resolved")
    (packs_dir(out) / "broken.yaml").write_text("{not: [valid")
    monkeypatch.setattr("auditor.pack_gc.mirror_paths", lambda *_: {})
    cp.run(out, runner=lambda *a: pytest.fail("a proven guard is not re-proven"))
    assert load_pack(out, pid)["status"] == "open"


def test_a_guard_record_without_a_commit_is_reported_not_a_crash(tmp_path: Path, monkeypatch, no_scans) -> None:
    out = tmp_path / "reports"
    _class_pack(out, {"proof_test": "tests/t.py", "proof": {"status": "pending"}})
    m = _git_mirror(tmp_path)
    monkeypatch.setattr("auditor.pack_gc.mirror_paths", lambda *_: {"r1": m})
    rep = cp.run(out, runner=lambda *a: pytest.fail("a malformed record is never proven"))
    assert [(p["repo"], p["status"]) for p in rep["proofs"]] == [("r1", "invalid")]
    assert "commit" in rep["proofs"][0]["note"]
    assert (out / cp.REPORT_NAME).is_file()
