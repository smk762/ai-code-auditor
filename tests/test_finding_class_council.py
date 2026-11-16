"""Label council (#53): voting rule, parsing, pack context, recording. Never calls the API."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor import finding_class as fc
from auditor import finding_class_council as council
from wiki.batch import Result, StageStats

V = set(fc.taxonomy().ids) | {"logic_error"}


def vote(verdict: str, better: str = "") -> dict:
    return {"verdict": verdict, "better": better, "why": ""}


@pytest.mark.parametrize("votes,outcome,final", [
    ([vote("agree"), vote("agree"), vote("disagree", "ssrf")], "confirmed", "logic_error"),
    ([vote("disagree", "ssrf"), vote("disagree", "ssrf"), vote("agree")], "relabelled", "ssrf"),
    ([vote("disagree", "ssrf"), vote("disagree", "injection"), vote("agree")], "contested", "logic_error"),
    ([vote("disagree", "ssrf"), vote("unclear"), None], "split", "logic_error"),   # one rejection is not enough
    ([vote("agree"), None, None], "split", "logic_error"),                         # failed members don't count
])
def test_decide(votes, outcome, final) -> None:
    d = council.decide("logic_error", votes)
    assert (d["outcome"], d["final"]) == (outcome, final)


def test_parse_verdicts_drops_bad_entries() -> None:
    text = 'ok {"verdicts": [{"i": 0, "verdict": "disagree", "better": "ssrf", "why": "url from client"},' \
           ' {"i": 1, "verdict": "maybe"}, {"i": 2, "verdict": "disagree", "better": "made_up"}, {"i": 7}]}'
    out = council.parse_verdicts(text, 3, V)
    assert out[0]["better"] == "ssrf" and out[1] is None and out[2] == {"verdict": "disagree", "better": "", "why": ""}
    assert council.parse_verdicts("nope", 2, V) == [None, None]


def test_members_see_the_label_and_the_cited_code(tmp_path: Path) -> None:
    (tmp_path / "p1.yaml").write_text(json.dumps({
        "location": {"file_path": "a.py", "code_snippet": "\n".join(f"line{i}" for i in range(50))},
        "narrative": {"description": "full description", "recommendation": "do this"}}))
    rows = [{"id": "p1", "title": "T1", "label": "ssrf", "set": "test"},
            {"id": "gone", "title": "T2", "desc": "short desc", "label": "injection", "set": "test"}]
    (req, idx), = council.build_requests(rows, "claude-haiku-5-5", "SYS", tmp_path)
    user = req.params["messages"][0]["content"]
    assert idx == [0, 1] and "Label: ssrf" in user and "full description" in user and "do this" in user
    assert "line29" in user and "line30" not in user          # snippet capped
    assert "short desc" in user                                # no pack: falls back to the gold row
    assert req.params["output_config"] == {"effort": "medium"}


class FakeRunner:
    def __init__(self, verdict):
        self.verdict = verdict

    def run(self, stage, requests):
        out = {}
        for req in requests:
            n = req.params["messages"][0]["content"].count("## Finding")
            out[req.custom_id] = Result(req.custom_id, stop_reason="end_turn", text=json.dumps(
                {"verdicts": [{"i": i, **self.verdict(stage, i)} for i in range(n)]}))
        return out, StageStats(requested=len(requests), cost_usd=0.01)


def test_run_records_outcomes_and_gold_follows(tmp_path: Path) -> None:
    gold = [{"id": "a", "title": "A", "label": "logic_error", "set": "test"},
            {"id": "b", "title": "B", "label": "ssrf", "set": "test"},
            {"id": "d", "title": "D", "label": "ssrf", "set": "dev"}]
    (tmp_path / "finding_classes_x.jsonl").write_text("\n".join(json.dumps(r) for r in gold))

    def verdict(stage, i):  # everyone moves 'a' to unhandled_error; 'b' is confirmed
        return {"verdict": "disagree", "better": "unhandled_error"} if i == 0 else {"verdict": "agree"}

    out = council.run(gold, tmp_path, tmp_path, runner_factory=lambda: FakeRunner(verdict), directory=tmp_path)
    assert out["n"] == 2 and out["outcomes"] == {"relabelled": 1, "confirmed": 1} and out["fidelity"] == 0.5
    assert out["cost_usd"] == pytest.approx(0.03)                      # one request per member
    rows = {r["id"]: r for r in fc.load_gold(tmp_path)}
    assert rows["a"]["label"] == "unhandled_error" and rows["a"]["label_first"] == "logic_error"
    assert rows["b"]["label"] == "ssrf" and rows["d"]["label"] == "ssrf"
    assert (tmp_path / council.SUMMARY_NAME).exists()

    dry = council.run(gold, tmp_path, tmp_path, dry_run=True, runner_factory=lambda: pytest.fail("sent"))
    assert dry["requests"] == 3 and dry["est_usd"] > 0
