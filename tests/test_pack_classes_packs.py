"""pack_classes.run: edits made during the model calls survive; paid answers survive a later failure."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from auditor import finding_class_llm as llm
from auditor import pack_classes
from auditor.agent_pack import load_pack, packs_dir
from wiki.batch import Result, StageStats


def _write(out: Path, pid: str, title: str) -> None:
    d = packs_dir(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.yaml").write_text(json.dumps({
        "work_item_id": pid, "kind": "nightly", "status": "open", "repo": {"name": "r"},
        "narrative": {"title": title, "description": "d"}, "location": {"file_path": "a.py", "line": 1},
        "governance": {}}))


class Runner:
    def __init__(self, calls: list[tuple[str, int]], hook: Callable[[str], None] = lambda stage: None):
        self.calls, self.hook = calls, hook

    def run(self, stage: str, requests: list[Any]) -> Any:
        self.hook(stage)
        self.calls.append((stage, len(requests)))
        out = {}
        for req in requests:
            lines = req.params["messages"][0]["content"].splitlines()[1:]
            out[req.custom_id] = Result(req.custom_id, stop_reason="end_turn", text=json.dumps(
                {"labels": [{"i": i, "class": "ssrf"} for i, _ in enumerate(lines)]}))
        return out, StageStats(requested=len(requests), cost_usd=0.01)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    llm.frozen_system(tmp_path, [{"id": "d", "title": "x", "label": "ssrf", "set": "dev"}])
    _write(tmp_path, "p1", "Callback URL enables SSRF")
    return tmp_path


def test_status_set_during_the_model_calls_is_kept(out: Path) -> None:
    from auditor.agent_pack import update_pack_status

    def claim_midway(stage: str) -> None:
        if stage.endswith(llm.MODELS[-1]):
            update_pack_status(out, "p1", "claimed")

    calls: list[tuple[str, int]] = []
    rep = pack_classes.run(out, runner_factory=lambda: Runner(calls, claim_midway))
    assert rep["written"] == 1
    data = load_pack(out, "p1")
    assert data["governance"]["finding_class"]["id"] == "ssrf"
    assert data["status"] == "claimed"


def test_answers_already_paid_for_survive_a_later_model_failure(out: Path) -> None:
    def fail_last(stage: str) -> None:
        if stage.endswith(llm.MODELS[-1]):
            raise RuntimeError("quota")

    calls: list[tuple[str, int]] = []
    with pytest.raises(RuntimeError, match="quota"):
        pack_classes.run(out, runner_factory=lambda: Runner(calls, fail_last))
    paid = [s for s, _ in calls]
    assert len(paid) == len(llm.MODELS) - 1

    calls.clear()
    rep = pack_classes.run(out, runner_factory=lambda: Runner(calls))
    assert len(calls) == 1 and calls[0][0].endswith(llm.MODELS[-1])  # only the failed model is asked again
    assert rep["written"] == 1
