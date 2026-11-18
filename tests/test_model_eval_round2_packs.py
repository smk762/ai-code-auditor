"""Regression test for the round-1 unit match in scripts/model_eval_round2.py (no model calls)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("model_eval_round2", ROOT / "scripts" / "model_eval_round2.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_unit_edited_in_place_is_not_reused() -> None:
    from auditor.contracts import CodeUnit

    r2 = _load()
    unit = CodeUnit("u1", "r", "a.py", "f", "function", "python", 10, 20, raw_text="new body", raw_text_hash="new")
    same = {"start_line": 10, "end_line": 20, "raw_text_hash": "new"}
    edited = {"start_line": 10, "end_line": 20, "raw_text_hash": "old"}  # same span, different code
    moved = {"start_line": 11, "end_line": 21, "raw_text_hash": "new"}
    assert r2.unchanged(unit, same)
    assert not r2.unchanged(unit, edited)
    assert not r2.unchanged(unit, moved)
    assert not r2.unchanged(unit, None)


def _fake_round(r2, tmp_path, monkeypatch, unit_ids, call):
    """Wire main() to two fake round-1 units and a fake model; no network, no spend."""
    import json
    from types import SimpleNamespace

    base = tmp_path / "base"
    base.mkdir()
    wanted = [{"id": u, "repo": "r", "start_line": 1, "end_line": 2, "raw_text_hash": "h"} for u in ("u1", "u2")]
    (base / "sample.json").write_text(json.dumps(wanted))
    (base / "raw.jsonl").write_text("")
    units = [SimpleNamespace(id=u, repo="r", start_line=1, end_line=2, raw_text_hash="h") for u in unit_ids]
    built = []

    class Client:
        def __init__(self, **kw):
            pass

        def build_prompt(self, unit, root, instructions=""):
            return f"prompt {unit.id}"

    monkeypatch.setattr(r2, "ROOT", tmp_path)
    monkeypatch.setattr(r2, "load_ecosystem_config", lambda: SimpleNamespace(repos=[SimpleNamespace(name="r")]))
    monkeypatch.setattr(r2, "resolve_scan_root", lambda repo: tmp_path)
    monkeypatch.setattr(r2, "build_repo_briefing", lambda *a, **kw: "")
    monkeypatch.setattr(r2, "scan_repo", lambda repo: ["f"])
    monkeypatch.setattr(r2, "extract_code_units", lambda repo, f: units)
    monkeypatch.setattr(r2, "LLMClient", Client)
    monkeypatch.setattr(r2, "_parse_findings_json", lambda text, unit: [])
    monkeypatch.setattr(r2.anthropic, "Anthropic", lambda **kw: built.append(1) or object())
    monkeypatch.setattr(r2.me, "call", call)
    monkeypatch.setattr(sys, "argv", ["model_eval_round2.py", "--base", str(base), "--concurrency", "2"])
    return built


def test_no_matching_units_exits_before_any_spend(tmp_path, monkeypatch) -> None:
    r2 = _load()
    calls = []
    built = _fake_round(r2, tmp_path, monkeypatch, [], lambda *a, **kw: calls.append(a) or {})
    assert r2.main() != 0
    assert built == [] and calls == []


def test_one_failing_unit_does_not_discard_the_others(tmp_path, monkeypatch) -> None:
    import json

    r2 = _load()

    def call(client, arm, content, max_tokens, schema=None):
        if arm.name == "haiku-v2" and "u1" in content:
            raise RuntimeError("unexpected")
        if arm.name == "haiku-v2":
            return {"text": "[]", "cost": 0.01}
        return {"text": json.dumps({"findings": [], "missed_issues": []}), "cost": 0.02}

    _fake_round(r2, tmp_path, monkeypatch, ["u1", "u2"], call)
    assert r2.main() == 0
    (out,) = (tmp_path / "reports" / "model_eval").iterdir()
    raw = [json.loads(line) for line in (out / "raw.jsonl").read_text().splitlines()]
    assert [r["unit"] for r in raw] == ["u2"]
    errors = [json.loads(line) for line in (out / "errors.jsonl").read_text().splitlines()]
    assert [(e["phase"], e["unit"]) for e in errors] == [("haiku-v2", "u1")]
    assert (out / "summary.md").exists()
    assert len((out / "judgements.jsonl").read_text().splitlines()) == 2
