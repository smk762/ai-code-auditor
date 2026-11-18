from __future__ import annotations

from pathlib import Path

import yaml

from patterns.pattern_feedback import append_architecture_rule_for_extraction

CANDIDATE = {
    "id": "ext-retr-001", "repos_involved": ["argus-cortex", "argus-studio"],
    "source_units": [{"repo": "argus-cortex", "path": "a.py", "symbols": ["retry"]}],
    "proposed_library": {"name": "retry-core", "owner_repo": "argus-cortex"},
}


def test_rule_append_to_empty_rules_key(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text("# starting point\nrules:\n")  # parses to {"rules": None}
    append_architecture_rule_for_extraction(CANDIDATE, str(rules))
    text = rules.read_text()
    assert text.startswith("# starting point\n")
    assert [r["name"] for r in yaml.safe_load(text)["rules"]] == ["use_retry_core"]
    # idempotent, and a non-dict entry in the list does not crash the name check
    rules.write_text(text + "  - just-a-string\n")
    append_architecture_rule_for_extraction(CANDIDATE, str(rules))
    assert next(r for r in yaml.safe_load(rules.read_text())["rules"] if isinstance(r, dict))["name"] == "use_retry_core"
