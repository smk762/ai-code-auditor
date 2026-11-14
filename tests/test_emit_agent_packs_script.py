"""scripts/emit_agent_packs.py replaces only the pack kinds it rebuilds."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("emit_agent_packs", ROOT / "scripts" / "emit_agent_packs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_remove_owned_packs_keeps_finding_packs(tmp_path: Path) -> None:
    packs = tmp_path / "agent_packs"
    packs.mkdir()
    for pid, kind in (("pack-w", "world_model"), ("pack-a", "architecture"), ("pack-e", "extraction"),
                      ("pack-n", "nightly"), ("pack-d", "diff_audit"), ("pack-b", "bug")):
        (packs / f"{pid}.yaml").write_text(yaml.safe_dump({"work_item_id": pid, "kind": kind}))
        (packs / f"{pid}.md").write_text("brief")
    (packs / "pack-bad.yaml").write_text(": : not yaml [")

    assert _script().remove_owned_packs(tmp_path) == 3
    assert sorted(p.name for p in packs.iterdir()) == [
        "pack-b.md", "pack-b.yaml", "pack-bad.yaml", "pack-d.md", "pack-d.yaml", "pack-n.md", "pack-n.yaml",
    ]
