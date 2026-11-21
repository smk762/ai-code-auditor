"""conductor.schemas.validate_status_dict: a non-list draft_prs is a validation error, not a crash."""

from __future__ import annotations

import pytest

from conductor.schemas import validate_status_dict


@pytest.mark.parametrize("value", [1, True, "abc", {"url": "x"}])
def test_non_list_draft_prs_is_reported(value: object) -> None:
    errors = validate_status_dict({"draft_prs": value})
    assert "draft_prs must be a list" in errors
    assert not any("action invalid" in e or "entries must be objects" in e for e in errors)
