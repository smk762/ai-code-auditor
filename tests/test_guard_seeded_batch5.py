"""guard_seeded: symlinked plant sites, leaked planted trees, bytes paths in the tracer and
malformed class packs (batch 5 packs b7ee8ec406, 7743857676, a0815ae033, 393dc28107)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from auditor import guard_seeded as gs
from auditor.agent_pack import packs_dir

SEED = gs.Seed(id="x", cls="c", code="def _seeded_x():\n    pass\n")
CLEAN_SRC = "import os\n\n\ndef f():\n    return os.sep\n"


def _runner(*, raise_after_base: bool = False):
    def run(clean: Path, base, tests):
        base()
        if raise_after_base:
            raise RuntimeError("the proof runner timed out")
        return SimpleNamespace(patched={"tests/t.py::t": "passed"}, repro=[])
    return run


def test_a_symlinked_plant_site_never_writes_through_the_link(tmp_path: Path) -> None:
    outside = tmp_path / "mirror_file.py"
    outside.write_text(CLEAN_SRC)
    clean = tmp_path / "clean"
    (clean / "pkg").mkdir(parents=True)
    (clean / "pkg" / "a.py").symlink_to(outside)
    work = tmp_path / "work"
    work.mkdir()
    r = gs._check_seed(SEED, clean, work, "pkg/a.py", ["tests/t.py"], None, _runner(), None)
    assert outside.read_text() == CLEAN_SRC
    assert r.status == "invalid" and "symlink" in r.note


def test_a_symlinked_plant_directory_never_writes_through_the_link(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.py").write_text(CLEAN_SRC)
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "pkg").symlink_to(outside, target_is_directory=True)
    work = tmp_path / "work"
    work.mkdir()
    r = gs._check_seed(SEED, clean, work, "pkg/a.py", ["tests/t.py"], None, _runner(), None)
    assert (outside / "a.py").read_text() == CLEAN_SRC
    assert r.status == "invalid"


def test_the_planted_tree_is_removed_when_the_runner_raises(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    (clean / "pkg").mkdir(parents=True)
    (clean / "pkg" / "a.py").write_text(CLEAN_SRC)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RuntimeError):
        gs._check_seed(SEED, clean, work, "pkg/a.py", ["tests/t.py"], None, _runner(raise_after_base=True), None)
    assert not (work / "planted-x").exists()


def test_the_tracer_survives_a_bytes_path(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "guard_scope_trace.py").write_text(gs._TRACER)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "data.txt").write_text("hi")
    (tree / "test_b.py").write_text(
        "import os\n\n\ndef test_reads_bytes_path():\n"
        "    p = os.fsencode(os.path.join(os.path.dirname(__file__), 'data.txt'))\n"
        "    with open(p) as fh:\n        assert fh.read() == 'hi'\n")
    out = tmp_path / "read.json"
    env = {**os.environ, "PYTHONPATH": str(plugin), "PYTHONDONTWRITEBYTECODE": "1",
           "GUARD_TRACE_ROOT": str(tree), "GUARD_TRACE_OUT": str(out)}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "guard_scope_trace",
                           "test_b.py"], cwd=tree, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "data.txt" in json.loads(out.read_text())


def _class_pack(d: Path, name: str, data: dict) -> None:
    (d / f"{name}.yaml").write_text(json.dumps({"kind": "class_guard", **data}))


def test_one_malformed_class_pack_never_aborts_the_scan(tmp_path: Path) -> None:
    d = packs_dir(tmp_path)
    d.mkdir(parents=True)
    proven = {"commit": "a" * 40, "proof_test": "tests/t.py", "proof": {"status": "proven"}}
    _class_pack(d, "pack-class-a", {"governance": {"class_id": "a", "class_guard": {"r1": proven}}})  # no id
    _class_pack(d, "pack-class-b", {"work_item_id": "pack-class-b", "governance": "oops"})
    _class_pack(d, "pack-class-c", {"work_item_id": "pack-class-c",
                                    "governance": {"class_id": "c", "class_guard": {"r1": {**proven, "proof": "x"},
                                                                                    "r2": proven}}})
    got = [(p, c, r) for p, c, r, _ in gs.proven_guards(tmp_path)]
    assert got == [("pack-class-a", "a", "r1"), ("pack-class-c", "c", "r2")]
