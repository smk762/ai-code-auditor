"""Finding classes (#53): taxonomy loading, rule classification and the gold-set eval.

Titles here are synthetic. The real gold set quotes live findings, so it stays outside this
public repo (``~/.config/ai-code-auditor/eval/``)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor import finding_class as fc
from auditor import finding_class_review as review
from auditor import trust_eval


def test_taxonomy_loads_and_every_class_has_a_guard() -> None:
    tax = fc.load_taxonomy()
    assert len(tax.ids) == len(set(tax.ids)) >= 15
    assert all(c.guard in {"ruff", "semgrep", "helper", "import_contract", "none"} for c in tax.classes)
    # FindingClass.title is the class's list of compiled title patterns (not a display name).
    assert all(c.title and all(p.pattern for p in c.title) for c in tax.classes), \
        "every class needs at least one non-empty title pattern"
    assert set(tax.static_rules["bandit"].values()) <= set(tax.ids)


def test_bad_taxonomy_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "t.yaml"
    p.write_text("classes:\n  - {id: a, title: [x]}\n  - {id: a, title: [y]}\n")
    with pytest.raises(ValueError, match="duplicate"):
        fc.load_taxonomy(p)
    p.write_text("classes:\n  - {id: a, title: [x]}\nstatic_rules:\n  bandit: {B1: nope}\n")
    with pytest.raises(ValueError, match="unknown classes"):
        fc.load_taxonomy(p)


@pytest.mark.parametrize("title,cls", [
    ("Client-controlled callback URL enables SSRF", "ssrf"),
    ("Upload name reaches the filesystem outside the root via path traversal", "path_traversal"),
    ("Admin routes fail open when the token env var is empty", "fail_open"),
    ("Service binds to all interfaces by default", "fail_open"),
    ("API key sent over plaintext HTTP to a configured host", "secret_exposure"),
    ("Provider keys stored unencrypted when the key is unset, so storage fails open", "secret_exposure"),
    ("Raw exception text returned to the client in the 502 body", "info_leak"),
    ("Unescaped filename lets an attacker inject a header", "injection"),
    ("Unbounded decode of client images allows memory exhaustion", "unbounded_resource"),
    ("Download call has no timeout", "missing_timeout"),
    ("Blocking file walks stall the event loop", "blocking_io_async"),
    ("Race condition between eviction and inference", "race_condition"),
    ("Zip handle is never closed", "resource_leak"),
    ("Non-atomic rewrite can destroy the file on interruption", "data_integrity"),
    ("Upstream failures are silently dropped from the summary", "silent_failure"),
    ("Path-traversal test can pass even if the server is vulnerable", "test_quality"),  # tests come first
    ("Selection is not reset when the job changes", "stale_state"),
    ("Empty list raises ValueError in the summary", "unhandled_error"),
    ("Seed field accepts unbounded values", "missing_validation"),  # not a resource problem
])
def test_classify(title: str, cls: str) -> None:
    assert fc.classify(title).cls == cls


@pytest.mark.parametrize("title", [
    "Race weights are type-checked",          # 'race' the field, not a race condition
    "check_text leaks a ValueError",          # 'leaks' an exception, not a resource
    "Wide inputs get a black band at the bottom",
])
def test_ambiguous_words_dont_misfire(title: str) -> None:
    assert fc.classify(title).cls not in {"race_condition", "resource_leak"}


def test_static_rule_ids_win() -> None:
    m = fc.classify("Something about a request", source="bandit", rule_id="B113")
    assert (m.cls, m.rule) == ("missing_timeout", "static:bandit:B113")
    assert fc.static_rule_id("bandit:B113:a.py:12", "bandit") == "B113"
    assert fc.static_rule_id("llm:u1:12", "bandit") == ""


def test_evaluate_reports_precision_coverage_and_mistakes(tmp_path: Path, monkeypatch) -> None:
    rows = [
        {"id": "1", "title": "Callback URL enables SSRF", "label": "ssrf", "set": "test"},
        {"id": "2", "title": "Zip handle is never closed", "label": "resource_leak", "set": "test"},
        {"id": "3", "title": "Empty list raises ValueError", "label": "logic_error", "set": "test"},  # rule wrong
        {"id": "4", "title": "Wrong rounding in the total", "label": "logic_error", "set": "test"},  # unclassified
        {"id": "5", "title": "Callback URL enables SSRF", "label": "ssrf", "set": "dev"},
    ]
    r = fc.evaluate(rows)
    t = r["sets"]["test"]
    assert (t["n"], t["classified"], t["right"]) == (4, 3, 2)
    assert t["precision"] == pytest.approx(2 / 3, abs=1e-3) and t["coverage"] == 0.75
    assert t["wrong"][0]["gold"] == "logic_error" and t["wrong"][0]["got"] == "unhandled_error"
    assert r["sets"]["dev"]["precision"] == 1.0

    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "finding_classes_x.jsonl").write_text("\n".join(json.dumps(x) for x in rows))
    monkeypatch.setenv("AI_AUDIT_EVAL_DIR", str(eval_dir))
    assert len(review.load_gold()) == 5
    out = tmp_path / "reports"
    out.mkdir()
    g = {x.id: x for x in trust_eval.build_gates(None, None, trust_eval.classes_eval(out))}
    assert g["classes.precision"].status == "insufficient" and g["classes.precision"].n == 3  # needs 30
    assert (out / fc.EVAL_NAME).exists()


def test_no_gold_set_means_insufficient(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_EVAL_DIR", str(tmp_path / "none"))
    assert trust_eval.classes_eval(tmp_path) is None
    g = {x.id: x for x in trust_eval.build_gates(None, None, None)}
    assert g["classes.precision"].status == "insufficient" and "no gold set" in g["classes.precision"].note
