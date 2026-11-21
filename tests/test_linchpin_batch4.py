"""linchpin.apply_labels keeps its record of the labels it added: a corrupt ledger stops the run
instead of being overwritten, and renaming the label back keeps ownership. No network: every
GitHub call goes through a fake *rest*."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor.linchpin import apply_labels


def _lp(repo: str, n: int, **kw) -> dict:
    return {"ref": f"{repo}#{n}", "slug": f"smk762/{repo}", "number": n, "labels": [], "external": False, **kw}


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe garbage", b"[]", b'"P0"', b"null"])
def test_unreadable_ledger_stops_the_run_and_is_left_alone(tmp_path: Path, content: bytes):
    calls: list[tuple[str, str]] = []
    ledger = tmp_path / "linchpin_labels.json"
    ledger.write_bytes(content)
    out = apply_labels({"linchpins": [_lp("argus-a", 1)]}, output_dir=tmp_path,
                       rest=lambda m, p, b=None: calls.append((m, p)))
    assert calls == []
    assert out["added"] == [] and out["removed"] == []
    assert out["errors"] and out["errors"][0]["ref"] == "*"
    assert ledger.read_bytes() == content


def test_missing_ledger_is_a_fresh_start(tmp_path: Path):
    calls: list[tuple[str, str]] = []
    out = apply_labels({"linchpins": [_lp("argus-a", 1)]}, output_dir=tmp_path,
                       rest=lambda m, p, b=None: calls.append((m, p)))
    assert out["added"] == ["argus-a#1"] and not out["errors"]
    assert set(json.loads((tmp_path / "linchpin_labels.json").read_text())["applied"]) == {"smk762/argus-a#1"}


def test_renaming_the_label_back_keeps_ownership_of_items_labelled_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, str]] = []
    ledger = tmp_path / "linchpin_labels.json"
    # Label was A, renamed to B; removing A from argus-a#1 failed, so it is still recorded as stale.
    ledger.write_text(json.dumps({"label": "B", "applied": {}, "stale": {"A": {"smk762/argus-a#1": "x"}}}))
    monkeypatch.setenv("AI_AUDIT_LINCHPIN_LABEL", "A")
    # Back to A, and argus-a#1 (still carrying A) is still P0: nothing to do, but it stays ours.
    out = apply_labels({"linchpins": [_lp("argus-a", 1, labels=["A"])]}, output_dir=tmp_path,
                       rest=lambda m, p, b=None: calls.append((m, p)))
    assert calls == [] and not out["errors"]
    saved = json.loads(ledger.read_text())
    assert saved["label"] == "A" and set(saved["applied"]) == {"smk762/argus-a#1"}
    # Once it is no longer P0, this tool takes its own label off.
    out = apply_labels({"linchpins": []}, output_dir=tmp_path, rest=lambda m, p, b=None: calls.append((m, p)))
    assert calls == [("DELETE", "/repos/smk762/argus-a/issues/1/labels/A")]
    assert out["removed"] == ["argus-a#1"]


def test_a_hand_applied_label_is_still_never_taken_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str]] = []
    ledger = tmp_path / "linchpin_labels.json"
    ledger.write_text(json.dumps({"label": "B", "applied": {}, "stale": {"A": {"smk762/argus-a#1": "x"}}}))
    monkeypatch.setenv("AI_AUDIT_LINCHPIN_LABEL", "A")
    apply_labels({"linchpins": [_lp("argus-h", 9, labels=["A"])]}, output_dir=tmp_path,
                 rest=lambda m, p, b=None: calls.append((m, p)))
    apply_labels({"linchpins": []}, output_dir=tmp_path, rest=lambda m, p, b=None: calls.append((m, p)))
    assert all("argus-h" not in p for _m, p in calls)
