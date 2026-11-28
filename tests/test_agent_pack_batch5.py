"""agent_pack briefs and bundle ordering (batch 5)."""
from __future__ import annotations

import pytest

from auditor.agent_pack import AgentCodingPack


def _pack(file_path: str, cmds: list[str]) -> AgentCodingPack:
    return AgentCodingPack(
        work_item_id="pack-x", kind="nightly", priority={"severity": "LOW", "score": 0.1},
        repo={"name": "r", "path": "", "base_branch": "dev"},
        location={"file_path": file_path, "line": 1, "symbol": "", "code_snippet": "", "evidence": ""},
        narrative={"title": "t", "description": "d", "recommendation": "r", "acceptance_criteria": []},
        impact={}, verification={"suggested_test_commands": cmds})


@pytest.mark.parametrize("cmd", ["pytest auditor/agent_pack.py", "pytest auditor/agent_pack.py -q"],
                         ids=["bare", "with_args"])
def test_pytest_on_a_source_file_is_dropped_from_the_brief(cmd: str) -> None:
    _, _, cmds = _pack("auditor/agent_pack.py", [cmd, "pytest -x --tb=short -q"])._brief_text()
    assert cmds == ["pytest -x --tb=short -q"]


def test_pytest_on_a_test_file_is_kept() -> None:
    _, _, cmds = _pack("tests/test_x.py", ["pytest tests/test_x.py"])._brief_text()
    assert cmds == ["pytest tests/test_x.py"]


def test_workaround_findings_count_as_consolidation_by_their_title() -> None:
    from auditor import workaround
    from auditor.agent_pack import _consolidates

    for title in workaround.TITLES:
        assert _consolidates({"title": title + "HACK: x"})
    assert not _consolidates({"title": "Something else"})
