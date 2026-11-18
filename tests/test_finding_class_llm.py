"""LLM finding classifier (#53): prompt, chunking, parsing, pricing; never calls the API."""
from __future__ import annotations

import json

import pytest

from auditor import finding_class as fc
from auditor import finding_class_llm as llm
from wiki.batch import Result, StageStats


def rows(n: int, *, label: str = "ssrf", split: str = "test") -> list[dict]:
    return [{"id": f"p{i}", "title": f"Finding {i} enables SSRF", "desc": "  long\n desc " * 40, "label": label,
             "set": split} for i in range(n)]


def test_system_prompt_lists_every_class_and_dev_examples_only() -> None:
    tax = fc.taxonomy()
    gold = [{"id": "d", "title": "Dev ssrf example", "label": "ssrf", "set": "dev"},
            {"id": "t", "title": "Held-out ssrf example", "label": "ssrf", "set": "test"}]
    ex = llm.few_shot(gold, tax)
    assert ex == {"ssrf": "Dev ssrf example"}  # never a held-out row
    system = llm.build_system(tax, ex)
    assert all(f"- {cid}:" in system for cid in tax.ids) and llm.UNCLEAR in system
    assert "Held-out" not in system and "Dev ssrf example → ssrf" in system


def test_requests_are_chunked_stable_and_bounded() -> None:
    r = rows(45)
    a = llm.build_requests(r, "claude-haiku-5-5", "SYS")
    b = llm.build_requests(r, "claude-haiku-5-5", "SYS")
    assert [len(idx) for _, idx in a] == [20, 20, 5]
    assert [x.custom_id for x, _ in a] == [x.custom_id for x, _ in b]  # reruns reuse results
    assert a[0][0].custom_id != llm.build_requests(r, "claude-sonnet-5-5", "SYS")[0][0].custom_id
    p = a[0][0].params
    assert p["output_config"] == {"effort": "low"} and "temperature" not in p and "thinking" not in p
    user = p["messages"][0]["content"]
    assert user.startswith("Findings:\n0. Finding 0") and "\n19. Finding 19" in user
    assert max(len(line) for line in user.splitlines()) < 220  # descriptions trimmed to DESC_CHARS


@pytest.mark.parametrize("text,expected", [
    ('{"labels": [{"i": 0, "class": "ssrf"}, {"i": 2, "class": "race_condition"}]}', ["ssrf", "", "race_condition"]),
    ('Here you go:\n{"labels": [{"i": 1, "class": "logic_error"}]}\nthanks', ["", "logic_error", ""]),
    ('{"labels": [{"i": 0, "class": "made_up"}, {"i": 1, "class": "unclear"}, {"i": 9, "class": "ssrf"}]}',
     ["", "", ""]),  # unknown class, unclear, out of range: unclassified
    ("not json at all", ["", "", ""]),
    ('{"labels": [{"i": "x", "class": "ssrf"}, null]}', ["", "", ""]),
])
def test_parse_labels(text: str, expected: list[str]) -> None:
    assert llm.parse_labels(text, 3, set(fc.taxonomy().ids) | llm.EXTRA_LABELS) == expected


class FakeRunner:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def run(self, stage, requests):
        self.calls.append(len(requests))
        out = {}
        for k, req in enumerate(requests):
            n = req.params["messages"][0]["content"].count("\n")
            out[req.custom_id] = self.answer(k, n)
        return out, StageStats(requested=len(requests), cost_usd=0.01 * len(requests))


def test_classify_rows_maps_labels_back_and_counts_failures() -> None:
    r = rows(25)
    ok = lambda k, n: Result("x", text=json.dumps({"labels": [{"i": i, "class": "ssrf"} for i in range(n)]}),
                             stop_reason="end_turn")
    out = llm.classify_rows(r, "claude-haiku-5-5", "/unused", examples_from=r, runner=FakeRunner(ok))
    assert out["preds"] == ["ssrf"] * 25 and out["failed"] == 0 and out["cost_usd"] == pytest.approx(0.02)

    flaky = lambda k, n: ok(k, n) if k == 0 else Result("x", error="batch result errored")
    out = llm.classify_rows(r, "claude-haiku-5-5", "/unused", examples_from=r, runner=FakeRunner(flaky))
    assert out["preds"][:20] == ["ssrf"] * 20 and out["preds"][20:] == [""] * 5 and out["failed"] == 1


def test_dry_run_prices_without_sending() -> None:
    out = llm.classify_rows(rows(45), "claude-opus-5-5", "/unused", examples_from=[], dry_run=True,
                            runner=FakeRunner(lambda k, n: pytest.fail("sent")))
    assert out["preds"] is None and out["requests"] == 3 and 0 < out["est_usd"] < 0.1


def test_evaluate_scores_any_predictor() -> None:
    gold = [{"id": "a", "title": "t", "label": "ssrf", "set": "test"},
            {"id": "b", "title": "t", "label": "logic_error", "set": "test"},
            {"id": "c", "title": "t", "label": "race_condition", "set": "test"}]
    rep = fc.evaluate(gold, predict=llm.predictor(gold, ["ssrf", "", "stale_state"]))
    t = rep["sets"]["test"]
    assert (t["classified"], t["right"], t["coverage"]) == (2, 1, pytest.approx(2 / 3, abs=1e-3))
    assert t["wrong"][0]["rule"] == "llm"


def test_runner_lane_and_dir_are_separate_from_the_wiki(tmp_path) -> None:
    from wiki.batch import Runner

    r = Runner(tmp_path, lane="classes", subdir="_classes")
    assert r.lane == "classes" and r.root == tmp_path / "_classes"
    w = Runner(tmp_path)
    assert w.lane == "wiki" and w.root == tmp_path / "wiki"


def test_save_gold_preds_interrupted_write_keeps_every_model(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from pathlib import Path

    rows = [{"id": "a"}, {"id": "b"}]
    llm.save_gold_preds(tmp_path, rows, "model-1", ["injection", "data_integrity"])
    path = llm.classes_dir(tmp_path) / llm.GOLD_PREDS_FILE
    real = Path.write_text

    def torn(self, text, *a, **kw):  # disk full / killed half way through the write
        real(self, text[: len(text) // 2], *a, **kw)
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", torn)
    with pytest.raises(OSError):
        llm.save_gold_preds(tmp_path, rows, "model-2", ["injection", "injection"])
    monkeypatch.undo()
    assert json.loads(path.read_text()) == {"model-1": {"a": "injection", "b": "data_integrity"}}
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []
    llm.save_gold_preds(tmp_path, rows, "model-2", ["injection", "injection"])
    assert set(json.loads(path.read_text())) == {"model-1", "model-2"}
