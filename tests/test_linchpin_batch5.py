"""linchpin.apply_labels: a ledger whose 'applied' or 'stale' is not an object stops the run
cleanly, before any GitHub call, and is left as it is."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("ledger", [
    {"label": "P0", "applied": {}, "stale": ["x"]},
    {"label": "P0", "applied": {}, "stale": "x"},
    {"label": "P0", "applied": ["smk762/argus-a#1"]},
    {"label": "P0", "applied": "x"},
], ids=["stale_list", "stale_str", "applied_list", "applied_str"])
def test_a_malformed_ledger_field_stops_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: dict) -> None:
    from auditor.linchpin import apply_labels

    monkeypatch.setenv("AI_AUDIT_LINCHPIN_LABEL", "P0")
    calls: list = []
    path = tmp_path / "linchpin_labels.json"
    path.write_text(json.dumps(ledger))
    before = path.read_bytes()
    lp = {"ref": "argus-a#1", "slug": "smk762/argus-a", "number": 1, "labels": [], "external": False}
    out = apply_labels({"linchpins": [lp]}, output_dir=tmp_path, rest=lambda m, p, b=None: calls.append((m, p)))
    assert calls == [] and out["added"] == [] and out["removed"] == []
    assert out["errors"] and out["errors"][0]["ref"] == "*"
    assert path.read_bytes() == before
