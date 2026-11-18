"""Regression tests for reporter audit packs."""
from __future__ import annotations

from pathlib import Path

from auditor.contracts import RuleViolation
from auditor.reporter import write_architecture_violations


def test_unknown_rule_violations_are_listed_in_the_markdown(tmp_path: Path) -> None:
    vs = [RuleViolation(rule_name="brand_new_rule", violating_path="argus-x/x.py", entities=["argus-x"],
                        evidence="why it broke", severity="LOW"),
          RuleViolation(rule_name="service_boundary", violating_path="a -> b", entities=["a", "b"], evidence="e")]
    md = write_architecture_violations(vs, str(tmp_path)).read_text()
    assert "- Total violations: 2" in md
    assert "  - Other violations: 1" in md
    assert "## Other Violations" in md and "### brand_new_rule" in md and "why it broke" in md


def test_only_unknown_rules_still_get_a_section(tmp_path: Path) -> None:
    vs = [RuleViolation(rule_name="typo_rule", violating_path="p", entities=[], evidence="e")]
    md = write_architecture_violations(vs, str(tmp_path)).read_text()
    assert "### typo_rule" in md
    assert "No architecture violations detected" not in md
