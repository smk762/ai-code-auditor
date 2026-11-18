"""scripts/emit_agent_packs.py: a failed regeneration must not lose the packs it was replacing."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    spec = importlib.util.spec_from_file_location("emit_agent_packs", ROOT / "scripts" / "emit_agent_packs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    packs = tmp_path / "agent_packs"
    packs.mkdir()
    for pid, kind, status in (("pack-w", "world_model", "claimed"), ("pack-stale", "architecture", "open"),
                              ("pack-n", "nightly", "open")):
        (packs / f"{pid}.yaml").write_text(yaml.safe_dump({"work_item_id": pid, "kind": kind, "status": status}))
        (packs / f"{pid}.md").write_text("brief")

    calls = {"rebuild_index": 0}

    def rebuild_index(out, **kw):
        calls["rebuild_index"] += 1
        return {"count": 0}

    cfg = SimpleNamespace(output_dir=str(tmp_path), repos=[], graph_db_path=str(tmp_path / "g.db"))
    monkeypatch.setattr(mod, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(mod, "public_base_url", lambda: "http://x")
    monkeypatch.setattr(mod, "pack_rag_ingest_enabled", lambda explicit=None: False)
    monkeypatch.setattr(mod, "emit_world_model_packs", lambda **kw: [SimpleNamespace(work_item_id="pack-w")])
    monkeypatch.setattr(mod, "load_architecture_rules", lambda: {})
    monkeypatch.setattr(mod, "GraphBuilder", lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(mod, "emit_packs_from_violations", lambda *a, **kw: [])
    monkeypatch.setattr(mod, "emit_packs_from_extraction_candidates", lambda **kw: [])
    monkeypatch.setattr(mod, "restore_pack_lifecycle", lambda *a, **kw: 0)
    monkeypatch.setattr(mod, "rebuild_index", rebuild_index)
    monkeypatch.setattr(mod, "list_packs", lambda *a, **kw: [])
    monkeypatch.setattr("auditor.class_packs.emit", lambda out, **kw: {})
    monkeypatch.setattr("auditor.pack_savings.refresh_quietly", lambda out: {})
    monkeypatch.setattr(sys, "argv", ["emit_agent_packs.py", "--quick", "--no-rag"])
    return mod, packs, calls


def test_a_failed_regeneration_keeps_the_existing_packs(env, monkeypatch) -> None:
    mod, packs, calls = env

    class Engine:
        def __init__(self, **kw):
            pass

        def evaluate(self):
            raise RuntimeError("graph db unreadable")

    monkeypatch.setattr(mod, "ArchitectureEngine", Engine)
    with pytest.raises(RuntimeError, match="graph db unreadable"):
        mod.main()
    assert sorted(p.name for p in packs.glob("*.yaml")) == ["pack-n.yaml", "pack-stale.yaml", "pack-w.yaml"]
    assert yaml.safe_load((packs / "pack-w.yaml").read_text())["status"] == "claimed"
    assert calls["rebuild_index"] == 1  # the index is rebuilt to match what is on disk


def test_a_successful_run_drops_only_owned_packs_it_no_longer_emits(env, monkeypatch) -> None:
    mod, packs, _ = env
    monkeypatch.setattr(mod, "ArchitectureEngine", lambda **kw: SimpleNamespace(evaluate=lambda: []))
    mod.main()
    assert sorted(p.name for p in packs.iterdir()) == ["pack-n.md", "pack-n.yaml", "pack-w.md", "pack-w.yaml"]


def test_sample_printing_survives_none_valued_index_fields(env, monkeypatch) -> None:
    mod, _, _ = env
    refreshed = []
    monkeypatch.setattr(mod, "ArchitectureEngine", lambda **kw: SimpleNamespace(evaluate=lambda: []))
    entry = {"id": "pack-x", "kind": "nightly", "title": None, "severity": None, "repo": None}
    monkeypatch.setattr(mod, "list_packs", lambda *a, **kw: [entry])
    monkeypatch.setattr("auditor.pack_savings.refresh_quietly", lambda out: refreshed.append(out) or {})
    mod.main()
    assert refreshed  # the trailing pack-savings refresh still ran
