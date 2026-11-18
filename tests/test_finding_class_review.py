"""Second-labeller review (#53): blind pass, unattributed adjudication, agreement, gold override.
Synthetic rows only; the real gold set stays outside this public repo."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor import finding_class as fc
from auditor import finding_class_review as rv

GOLD = [
    {"id": "a", "title": "Callback URL enables SSRF", "desc": "secret-first-label-hint", "label": "ssrf", "set": "test"},
    {"id": "b", "title": "Zip handle never closed", "label": "resource_leak", "set": "test"},
    {"id": "c", "title": "Wrong rounding", "label": "logic_error", "set": "test"},
    {"id": "d", "title": "Dev only", "label": "ssrf", "set": "dev"},
]


def scripted(*answers: str):
    it = iter(answers)
    return lambda _prompt: next(it)


def write_gold(d: Path) -> None:
    (d / "finding_classes_x.jsonl").write_text("\n".join(json.dumps(r) for r in GOLD))


def test_blind_pass_never_shows_the_first_label_and_resumes(tmp_path: Path) -> None:
    shown: list[str] = []
    n = rv.run_blind(GOLD, directory=tmp_path, ask=scripted("ssrf", "q"), out=shown.append)
    assert n == 1
    text = "\n".join(shown)
    assert "Dev only" not in text                     # held-out set only
    for r in GOLD[:3]:                                 # the first label is never printed next to its finding
        assert f"label: {r['label']}" not in text and f"A: {r['label']}" not in text
    first_id = next(iter(rv.load_reviews(tmp_path)))
    left = rv.blind_queue(GOLD, rv.load_reviews(tmp_path))
    assert len(left) == 2 and first_id not in {r["id"] for r in left}  # resumes where it stopped

    rv.run_blind(GOLD, directory=tmp_path, ask=scripted("bogus", "u", "s"), out=lambda _: None)
    labels = {k: v.get("second") for k, v in rv.load_reviews(tmp_path).items()}
    assert sorted(labels.values()) == ["ssrf", "unclear"]  # the bogus answer was refused, the skip not saved


def test_number_answers_follow_the_menu(tmp_path: Path) -> None:
    choices = rv._choices(fc.taxonomy())
    assert rv._parse("1", choices) == choices[0] and rv._parse(str(len(choices) + 1), choices) is None
    assert rv._parse("logic_error", choices) == "logic_error" and rv._parse("u", choices) == rv.UNCLEAR


def adjudicated(tmp_path: Path, second: dict[str, str], finals: list[str]) -> dict:
    for rid, lab in second.items():
        rv._append(tmp_path, {"id": rid, "second": lab})
    shown: list[str] = []
    rv.run_adjudicate(GOLD, directory=tmp_path, seed=1, ask=scripted(*finals), out=shown.append)
    return {"shown": "\n".join(shown), "reviews": rv.load_reviews(tmp_path)}


def test_adjudication_only_asks_about_disagreements_unattributed(tmp_path: Path) -> None:
    out = adjudicated(tmp_path, {"a": "ssrf", "b": "data_integrity", "c": "unhandled_error"}, ["a", "logic_error"])
    assert "[1/2]" in out["shown"] and "Callback URL" not in out["shown"]  # 'a' agreed: never asked
    assert "first" not in out["shown"].lower() and "yours" not in out["shown"].lower()
    # Both labels appear, unattributed, as A and B.
    assert ("A: resource_leak" in out["shown"]) != ("B: resource_leak" in out["shown"])
    finals = {k: v["final"] for k, v in out["reviews"].items() if "final" in v}
    assert set(finals) == {"b", "c"}
    queue = rv.disputes(GOLD, {"a": {"second": "ssrf"}, "b": {"second": "x"}, "c": {"second": "y"}})
    second = queue[1]["id"]
    assert finals[second] == "logic_error"                  # a class id picks a third label
    first = queue[0]["id"]
    assert finals[first] in {GOLD[1]["label"], "data_integrity"} if first == "b" else \
        finals[first] in {"logic_error", "unhandled_error"}  # 'a' picked one of the pair


def test_agreement_and_kappa(tmp_path: Path) -> None:
    reviews = {"a": {"second": "ssrf"}, "b": {"second": "data_integrity"}, "c": {"second": "logic_error"}}
    ag = rv.agreement(GOLD, reviews)
    assert (ag["n"], ag["agree"], ag["disputed"]) == (3, 2, 1)
    assert ag["kappa"] == pytest.approx((2 / 3 - 2 / 9) / (1 - 2 / 9), abs=1e-3)  # chance: ssrf + logic_error
    assert (ag["guard_n"], ag["guard_agree"]) == (2, 1)  # 'c' is logic_error on both sides: no guard
    assert rv.agreement(GOLD, {})["n"] == 0


def test_gold_uses_the_adjudicated_label(tmp_path: Path) -> None:
    write_gold(tmp_path)
    rv._append(tmp_path, {"id": "b", "second": "data_integrity"})
    rv._append(tmp_path, {"id": "b", "final": "data_integrity"})
    rv._append(tmp_path, {"id": "c", "second": "unclear"})
    rv._append(tmp_path, {"id": "c", "final": "unclear"})
    rows = {r["id"]: r for r in fc.load_gold(tmp_path)}
    assert rows["b"]["label"] == "data_integrity" and rows["b"]["label_first"] == "resource_leak"
    assert "c" not in rows                                  # settled as unclear: no usable label
    assert rows["a"]["label"] == "ssrf"                     # never disputed: unchanged
    assert {r["id"] for r in fc.load_gold(tmp_path, reviewed=False)} == {"a", "b", "c", "d"}
    assert not list(tmp_path.glob("finding_classes_review*"))  # the review file is never read as gold


def test_load_reviews_skips_corrupt_lines_and_append_heals_a_torn_tail(tmp_path: Path):
    rv._append(tmp_path, {"id": "a", "final": "injection"})
    p = rv.review_path(tmp_path)
    with p.open("a", encoding="utf-8") as f:
        f.write("[1, 2]\n{\"second\": \"x\"}\n{\"id\": \"b\", \"fin")  # no id, wrong type, torn write
    rv._append(tmp_path, {"id": "c", "second": "data_integrity"})
    reviews = rv.load_reviews(tmp_path)
    assert reviews == {"a": {"final": "injection"}, "c": {"second": "data_integrity"}}
