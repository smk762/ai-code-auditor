"""Approved rules live outside git and the rules loader merges them; old extraction approvals are dropped."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from auditor.config import approved_rules_path, load_architecture_rules

BASE = {"rules": [{"name": "no_cycles", "description": "d",
                   "forbid": [{"from_roles": ["library"], "to_roles": ["worker"]}]}],
        "service_roles": [{"name": "argus-cortex", "role": "library"}, {"name": "argus-lens", "role": "worker"}],
        "call_graph_rules": {}}


def test_default_path_is_outside_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_AUDIT_ARCH_RULES_PATH", raising=False)
    assert approved_rules_path() == "rules/architecture_rules.json"


APPROVED = {"name": "lens_never_imports_cortex", "description": "d",
            "forbid": [{"from_roles": ["worker"], "to_roles": ["library"]}]}
OLD_EXTRACTION = {"name": "use_shared_logic", "description": "All related logic should use shared-logic.",
                  "required_imports": ["shared_logic"], "forbidden_patterns": ["custom_sha256"],
                  "applies_to": ["argus-cortex"]}


def test_approved_rules_are_merged_on_load(tmp_path: Path) -> None:
    base = tmp_path / "architecture_rules.yaml"
    base.write_text(yaml.safe_dump(BASE))
    approved = tmp_path / "rules" / "architecture_rules.json"
    approved.parent.mkdir()
    approved.write_text(json.dumps({"rules": [APPROVED]}))
    names = [r["name"] for r in load_architecture_rules(base, approved=approved, repos={"argus-cortex", "argus-lens"})["rules"]]
    assert names == ["no_cycles", "lens_never_imports_cortex"]
    assert base.read_text() == yaml.safe_dump(BASE)  # the checked-in file is untouched


def test_an_old_extraction_approval_is_dropped_not_loaded(tmp_path: Path, caplog) -> None:
    """The old approve-extraction wrote rules no evaluator read: they checked nothing, and the
    arch.contracts gate counted each as a rule that never fires."""
    base = tmp_path / "architecture_rules.yaml"
    base.write_text(yaml.safe_dump(BASE))
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps({"rules": [OLD_EXTRACTION, APPROVED]}))
    names = [r["name"] for r in load_architecture_rules(base, approved=approved, repos={"argus-cortex", "argus-lens"})["rules"]]
    assert names == ["no_cycles", "lens_never_imports_cortex"]
    assert "use_shared_logic" in caplog.text


def test_a_checked_in_rule_with_only_unevaluated_keys_is_a_load_error(tmp_path: Path) -> None:
    base = tmp_path / "architecture_rules.yaml"
    base.write_text(yaml.safe_dump({**BASE, "rules": [*BASE["rules"], OLD_EXTRACTION]}))
    with pytest.raises(ValueError, match="use_shared_logic.*never evaluated"):
        load_architecture_rules(base, approved=tmp_path / "none.json", repos={"argus-cortex", "argus-lens"})


def test_an_approved_rule_never_shadows_a_checked_in_one(tmp_path: Path) -> None:
    base = tmp_path / "architecture_rules.yaml"
    base.write_text(yaml.safe_dump({**BASE, "rules": [APPROVED]}))
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps({"rules": [{**APPROVED, "description": "ignored"}]}))
    [rule] = load_architecture_rules(base, approved=approved, repos={"argus-cortex", "argus-lens"})["rules"]
    assert rule["description"] == "d"


def test_missing_approved_file_changes_nothing(tmp_path: Path) -> None:
    base = tmp_path / "architecture_rules.yaml"
    base.write_text(yaml.safe_dump(BASE))
    assert load_architecture_rules(base, approved=tmp_path / "nope.json")["rules"] == BASE["rules"]


def test_compose_mounts_rules_for_every_service() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    for name, svc in compose["services"].items():
        assert "./rules:/app/rules" in (svc.get("volumes") or []), name
    assert Path("rules/.gitkeep").exists()  # so the mount isn't created by Docker as root
