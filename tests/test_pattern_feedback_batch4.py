from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from patterns.pattern_feedback import _insert_rule_text, _rule_for_extraction, append_architecture_rule_for_extraction

CANDIDATE = {
    "repos_involved": ["argus-cortex"],
    "source_units": [{"repo": "argus-cortex", "path": "a.py", "symbols": ["retry"]}],
    "proposed_library": {"name": "retry-core"},
}


def test_column_zero_rules_list_gets_a_column_zero_item_and_keeps_comments(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text("# keep me\nrules:\n- name: existing\n  applies_to: [a]\nother: 1\n")
    append_architecture_rule_for_extraction(CANDIDATE, str(rules))
    text = rules.read_text()
    assert text.startswith("# keep me\n")  # appended as text, not a whole-document rewrite
    assert "\n- name: use_retry_core\n" in text
    data = yaml.safe_load(text)
    assert [r["name"] for r in data["rules"]] == ["existing", "use_retry_core"] and data["other"] == 1


@pytest.mark.parametrize("marker", ["---", "..."])
def test_document_markers_end_the_rules_list(marker: str) -> None:
    text = f"rules:\n  - name: existing\n{marker}\nlater: doc\n"
    out = _insert_rule_text(text, {"name": "new"})
    head, tail = out.split(f"\n{marker}\n")
    assert "- name: new" in head and "new" not in tail


@pytest.mark.parametrize("candidate,lib,symbol", [
    ({"source_units": []}, "shared_lib", "custom_impl"),
    ({"source_units": [{"symbols": []}]}, "shared_lib", "custom_impl"),
    ({"source_units": [None]}, "shared_lib", "custom_impl"),
    ({"proposed_library": None, "repos_involved": None}, "shared_lib", "custom_impl"),
    ({"proposed_library": {"name": None}, "source_units": [{"symbols": ["retry"]}]}, "shared_lib", "retry"),
])
def test_empty_or_null_candidate_fields_fall_back(candidate: dict, lib: str, symbol: str) -> None:
    rule = _rule_for_extraction(candidate)
    assert rule["name"] == f"use_{lib}"
    assert rule["forbidden_patterns"] == [f"custom_{symbol}"]
    assert rule["applies_to"] == []
