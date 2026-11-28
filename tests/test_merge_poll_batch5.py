"""merge_poll.load_state: a malformed 'repos' is unreadable state, never a silent fresh start."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("repos", [["argus-a"], "argus-a", 3], ids=["list", "str", "int"])
def test_a_malformed_repos_field_is_unreadable_in_strict_mode(tmp_path: Path, repos) -> None:
    from auditor.merge_poll import StateUnreadable, load_state

    p = tmp_path / "state.json"
    p.write_text(json.dumps({"repos": repos, "updated_at": "x"}))
    with pytest.raises(StateUnreadable, match="repos"):
        load_state(p, strict=True)
    assert load_state(p)["repos"] == {}  # the pages still show it, as empty


def test_a_missing_or_null_repos_is_an_empty_state(tmp_path: Path) -> None:
    from auditor.merge_poll import load_state

    p = tmp_path / "state.json"
    for body in ({"updated_at": "x"}, {"repos": None}):
        p.write_text(json.dumps(body))
        assert load_state(p, strict=True)["repos"] == {}
