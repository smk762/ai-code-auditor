"""Regression tests for auditor/pr_intel.py (release detection, timestamps, report cache I/O)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from auditor import pr_intel
from auditor.pr_model import days_since


@pytest.mark.parametrize(
    "title",
    ["chore(release): v1.0.0", "chore(release) 1.2.0", "Release 1.0", "release: v2"],
)
def test_release_titles_are_detected(title):
    assert pr_intel._is_release(title, "feat/x", "dev")


@pytest.mark.parametrize("title", ["Releases are slow", "chore: bump deps", "fix(release-notes): typo"])
def test_non_release_titles_are_not(title):
    assert not pr_intel._is_release(title, "feat/x", "dev")


def test_days_since_mixes_naive_and_aware_without_crashing():
    aware_now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert days_since("2026-01-01T00:00:00", aware_now) == pytest.approx(1.0)
    naive_now = datetime(2026, 1, 2)
    assert days_since("2026-01-01T00:00:00Z", naive_now) == pytest.approx(1.0)
    assert days_since(None, aware_now) == 0.0


@pytest.mark.parametrize("body", ["[1, 2]", '"text"', "3", "null"])
def test_non_object_report_reads_as_none(tmp_path: Path, body: str):
    (tmp_path / pr_intel.REPORT_NAME).write_text(body, encoding="utf-8")
    assert pr_intel.load_report(tmp_path) is None
    assert pr_intel.load_report(tmp_path, max_age_h=1) is None


def test_overlapping_refreshes_both_land_a_whole_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import auditor.linchpin as linchpin
    import auditor.merge_poll as merge_poll

    monkeypatch.setattr(merge_poll, "list_poll_targets", lambda *_a, **_k: [])
    monkeypatch.setattr(linchpin, "labels_enabled", lambda: False)
    monkeypatch.setattr(pr_intel, "github_graphql", lambda tok: None)
    calls = {"n": 0}

    def build_report(targets, gql):
        calls["n"] += 1
        return {"generated_at": "2026-01-01T00:00:00+00:00", "call": calls["n"]}

    monkeypatch.setattr(pr_intel, "build_report", build_report)

    real_replace = os.replace
    state = {"nested": False}

    def interleaving_replace(src, dst):
        # Another refresh runs to completion between this one's temp write and its rename.
        if not state["nested"]:
            state["nested"] = True
            pr_intel.refresh(output_dir=tmp_path, token="t", label=False)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", interleaving_replace)
    pr_intel.refresh(output_dir=tmp_path, token="t", label=False)

    report = json.loads((tmp_path / pr_intel.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["call"] in (1, 2)
    assert not list(tmp_path.glob("*.tmp"))
