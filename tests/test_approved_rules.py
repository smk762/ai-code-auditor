"""Approving an extraction candidate writes outside git and the rules loader picks it up."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from auditor.config import approved_rules_path, load_architecture_rules
from patterns.pattern_feedback import append_architecture_rule_for_extraction

CANDIDATE = {"id": "ext-1", "proposed_library": {"name": "shared-logic"}, "repos_involved": ["argus-cortex", "argus-lens"],
             "source_units": [{"symbols": ["_now"]}]}
BASE = {"rules": [{"name": "no_cycles", "description": "d",
                   "forbid": [{"from_roles": ["library"], "to_roles": ["worker"]}]}],
        "service_roles": [{"name": "argus-cortex", "role": "library"}, {"name": "argus-lens", "role": "worker"}],
        "call_graph_rules": {}}


def test_default_path_is_outside_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_AUDIT_ARCH_RULES_PATH", raising=False)
    assert approved_rules_path() == "rules/architecture_rules.json"


def test_json_approval_is_idempotent_and_merged_on_load(tmp_path: Path) -> None:
    base = tmp_path / "architecture_rules.yaml"
    base.write_text(yaml.safe_dump(BASE))
    approved = tmp_path / "rules" / "architecture_rules.json"
    approved.parent.mkdir()
    append_architecture_rule_for_extraction(CANDIDATE, str(approved))
    append_architecture_rule_for_extraction(CANDIDATE, str(approved))
    assert [r["name"] for r in json.loads(approved.read_text())["rules"]] == ["use_shared_logic"]
    assert base.read_text() == yaml.safe_dump(BASE)  # the checked-in file is untouched
    names = [r["name"] for r in load_architecture_rules(base, approved=approved)["rules"]]
    assert names == ["no_cycles", "use_shared_logic"]


def test_an_approved_rule_never_shadows_a_checked_in_one(tmp_path: Path) -> None:
    base = tmp_path / "architecture_rules.yaml"
    base.write_text(yaml.safe_dump({"rules": [{"name": "use_shared_logic", "description": "kept",
                                                "required_imports": ["shared_logic"]}]}))
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps({"rules": [{"name": "use_shared_logic", "description": "ignored",
                                                       "required_imports": ["shared_logic"]}]}))
    [rule] = load_architecture_rules(base, approved=approved)["rules"]
    assert rule["description"] == "kept"


def test_missing_approved_file_changes_nothing(tmp_path: Path) -> None:
    base = tmp_path / "architecture_rules.yaml"
    base.write_text(yaml.safe_dump(BASE))
    assert load_architecture_rules(base, approved=tmp_path / "nope.json")["rules"] == BASE["rules"]


def test_compose_mounts_rules_for_every_service() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    for name, svc in compose["services"].items():
        assert "./rules:/app/rules" in (svc.get("volumes") or []), name
    assert Path("rules/.gitkeep").exists()  # so the mount isn't created by Docker as root
