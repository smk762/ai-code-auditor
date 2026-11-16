"""Classes onto packs (#53): consensus, the frozen prompt, the cache, write and remove. No API."""
from __future__ import annotations

import json
from pathlib import Path

from auditor import finding_class as fc
from auditor import finding_class_llm as llm
from auditor import pack_classes, trust_eval
from auditor.agent_pack import load_pack, packs_dir
from wiki.batch import Result, StageStats


def test_consensus_needs_every_model() -> None:
    assert llm.consensus(["ssrf", "ssrf", "ssrf"]) == "ssrf"
    assert llm.consensus(["ssrf", "ssrf", "injection"]) == ""
    assert llm.consensus(["", "", ""]) == "" and llm.consensus([]) == ""


def test_only_guardable_consensus_is_written() -> None:
    assert pack_classes.decide(dict.fromkeys(llm.MODELS, "ssrf")) == "ssrf"
    assert pack_classes.decide(dict.fromkeys(llm.MODELS, "logic_error")) == ""
    none_guard = next(c.id for c in fc.taxonomy().classes if c.guard == "none")
    assert pack_classes.decide(dict.fromkeys(llm.MODELS, none_guard)) == ""
    assert pack_classes.decide({**dict.fromkeys(llm.MODELS, "ssrf"), llm.MODELS[0]: "injection"}) == ""


def test_frozen_prompt_is_written_from_gold_and_read_without_it(tmp_path: Path) -> None:
    assert llm.frozen_system(tmp_path) is None              # nothing frozen, no gold: no prompt at all
    gold = [{"id": "d", "title": "Dev ssrf example", "label": "ssrf", "set": "dev"}]
    s = llm.frozen_system(tmp_path, gold)
    assert "Dev ssrf example" in s and llm.frozen_system(tmp_path) == s


def test_gate_scores_consensus_from_stored_answers(tmp_path: Path, monkeypatch) -> None:
    gold = [{"id": str(i), "title": f"T{i}", "label": "ssrf", "set": "test"} for i in range(4)]
    ed = tmp_path / "eval"
    ed.mkdir()
    (ed / "finding_classes_t.jsonl").write_text("\n".join(json.dumps(r) for r in gold))
    monkeypatch.setenv("AI_AUDIT_EVAL_DIR", str(ed))
    assert trust_eval.classes_eval(tmp_path)["classifier"] == "rules"
    for m in llm.MODELS:
        llm.save_gold_preds(tmp_path, gold, m, ["ssrf", "ssrf", "injection" if m == llm.MODELS[0] else "ssrf", ""])
    rep = trust_eval.classes_eval(tmp_path)
    t = rep["sets"]["test"]
    assert rep["classifier"] == "consensus" and (t["classified"], t["right"]) == (2, 2)


def write(out: Path, pid: str, title: str, kind: str = "nightly") -> None:
    d = packs_dir(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.yaml").write_text(json.dumps({
        "work_item_id": pid, "kind": kind, "status": "open", "repo": {"name": "r"},
        "narrative": {"title": title, "description": "d"}, "location": {"file_path": "a.py", "line": 1},
        "governance": {}}))


class Fake:
    calls = 0

    def __init__(self, answer):
        self.answer = answer

    def run(self, stage, requests):
        Fake.calls += len(requests)
        out = {}
        for req in requests:
            lines = req.params["messages"][0]["content"].splitlines()[1:]
            out[req.custom_id] = Result(req.custom_id, stop_reason="end_turn", text=json.dumps(
                {"labels": [{"i": i, "class": self.answer(stage, line)} for i, line in enumerate(lines)]}))
        return out, StageStats(requested=len(requests), cost_usd=0.01)


def test_run_writes_caches_and_removes(tmp_path: Path) -> None:
    llm.frozen_system(tmp_path, [{"id": "d", "title": "x", "label": "ssrf", "set": "dev"}])
    write(tmp_path, "p-ssrf", "Callback URL enables SSRF")
    write(tmp_path, "p-split", "Something ambiguous")
    write(tmp_path, "p-world", "World model", kind="world_model")

    def answer(stage, line):
        if "SSRF" in line:
            return "ssrf"
        return "injection" if stage.endswith("haiku-5-5") else "missing_validation"

    Fake.calls = 0
    rep = pack_classes.run(tmp_path, runner_factory=lambda: Fake(answer))
    assert rep["packs"] == 2 and rep["written"] == 1 and rep["by_class"] == {"ssrf": 1}
    fc_ = load_pack(tmp_path, "p-ssrf")["governance"]["finding_class"]
    assert fc_["id"] == "ssrf" and fc_["by"] == pack_classes.BY and fc_["guard"]
    assert "finding_class" not in (load_pack(tmp_path, "p-split")["governance"] or {})
    assert json.loads((packs_dir(tmp_path) / "INDEX.json").read_text())["packs"]

    calls = Fake.calls
    again = pack_classes.run(tmp_path, runner_factory=lambda: Fake(answer))
    assert again["classified_now"] == 0 and Fake.calls == calls and again["written"] == 0   # cached: free

    write(tmp_path, "p-ssrf", "Callback URL is logged")  # text changed: asked again, class dropped
    pack_classes.run(tmp_path, runner_factory=lambda: Fake(answer))
    assert "finding_class" not in (load_pack(tmp_path, "p-ssrf")["governance"] or {})


def test_no_frozen_prompt_skips(tmp_path: Path) -> None:
    assert "skipped" in pack_classes.run(tmp_path)
