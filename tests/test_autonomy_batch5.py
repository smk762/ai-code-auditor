"""autonomy.load_config: every bad config is a ValueError naming the file, and an override can
pin a scope below the floor but never lift it above the ceiling."""
from __future__ import annotations

from pathlib import Path

import pytest


def _load(tmp_path: Path, text: str):
    from auditor import autonomy as au

    p = tmp_path / "autonomy.yaml"
    p.write_text(text)
    return au.load_config(p)


@pytest.mark.parametrize("text", [
    "default: [draft_pr\n",
    "thresholds: [0.1, 0.2]\n",
    "thresholds: 0.5\n",
    "irreversible: [auth]\n",
    "irreversible: secrets\n",
    "overrides:\n  - {repo: a, level: auto_merge, reason: trust me}\n",
    "ceiling: draft_pr\noverrides:\n  - {repo: a, level: ready_pr, reason: trust me}\n",
], ids=["broken_yaml", "thresholds_list", "thresholds_scalar", "irreversible_list", "irreversible_str",
        "override_above_default_ceiling", "override_above_ceiling"])
def test_a_bad_config_is_a_value_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError, match="autonomy.yaml"):
        _load(tmp_path, text)


def test_a_directory_is_a_value_error(tmp_path: Path) -> None:
    from auditor import autonomy as au

    with pytest.raises(ValueError):
        au.load_config(tmp_path)


def test_an_override_may_still_pin_a_scope_to_report(tmp_path: Path) -> None:
    c = _load(tmp_path, "overrides:\n  - {repo: a, level: report, reason: release freeze}\n")
    assert c.overrides[0]["level"] == "report"
