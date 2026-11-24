"""Briefs name the fix and the repo-relative file, and stored packs keep the text class guards match on."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from auditor import rule_fixes, static_tools
from auditor.agent_pack import build_pack_from_finding
from auditor.contracts import Finding


def _pack(root: Path, rule: str = "B602", rec: str = rule_fixes.GENERIC, source: str = "bandit",
          rel: str = "app/run.py"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import subprocess\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return build_pack_from_finding(
        Finding(id=f"{source}:{rule}:{path}:1", type="security", severity="HIGH", repo="x", file_path=str(path), line=1,
                title="t", description="d", evidence="e", recommendation=rec, source=source), repo_path=root)


@pytest.mark.parametrize("folder", ["x", "checkout"])
def test_briefs_show_a_repo_relative_path(tmp_path: Path, folder: str) -> None:
    pack = _pack(tmp_path / folder)
    for text in (pack.to_brief_md(), pack.to_compact_md()):
        assert "`app/run.py`:1" in text and str(tmp_path) not in text
    assert pack.location["file_path"] == str(tmp_path / folder / "app/run.py")  # ids and dedupe use it as stored


def test_the_brief_gives_the_rules_fix_and_the_pack_keeps_the_generic_text(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    for text in (pack.to_brief_md(), pack.to_compact_md()):
        assert "drop `shell=True`" in text and rule_fixes.GENERIC not in text
    assert pack.narrative["recommendation"] == rule_fixes.GENERIC  # class guards match on the stored words


def test_other_recommendations_are_left_alone(tmp_path: Path) -> None:
    assert "Use the helper." in _pack(tmp_path, rec="Use the helper.").to_brief_md()
    assert rule_fixes.GENERIC in _pack(tmp_path, rule="B999").to_brief_md()
    assert rule_fixes.GENERIC in _pack(tmp_path, source="llm").to_brief_md()


def test_a_source_file_is_not_offered_as_a_test_command(tmp_path: Path) -> None:
    text = _pack(tmp_path / "a").to_brief_md()
    assert "pytest app/run.py" not in text and "pytest -x" in text
    assert "`pytest tests/test_run.py -q" in _pack(tmp_path / "b", rel="tests/test_run.py").to_brief_md()


def test_fixes_are_keyed_by_rule_and_bandit_findings_carry_the_generic_text() -> None:
    assert all(re.fullmatch(r"B\d{3}", k) and v.strip() for k, v in rule_fixes.BANDIT.items())
    assert "rule_fixes.GENERIC" in Path(static_tools.__file__).read_text(encoding="utf-8")
