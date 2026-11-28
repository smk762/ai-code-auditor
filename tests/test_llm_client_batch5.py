"""A findings array survives other brackets around it in the model's output."""
from __future__ import annotations

from typing import Any

import pytest

import auditor.llm_client as lc

F = [{"title": "x"}]


@pytest.mark.parametrize("raw,expected", [
    ('{"findings": [{"title": "x"}], "notes": []}', F),
    ('{"notes": [], "findings": [{"title": "x"}]}', F),
    ('See items[0] in the loop.\n```json\n[{"title": "x"}]\n```', F),
    ('Line [12] is wrong:\n[{"title": "x"}]', F),
    ('[{"title": "x"}]\nAlso check foo[1].', F),
    ('Nothing found: [] (checked a[0])', []),
    ('[{"title": "x"}]', F),
    ('{"note": "nothing"}', None),
    ("no array here", None),
], ids=["wrapper_two_arrays", "wrapper_findings_second", "prose_bracket_then_fence", "prose_bracket_before",
        "trailing_bracket", "empty_array_in_prose", "plain", "object_without_array", "prose_only"])
def test_the_findings_array_is_found_among_other_brackets(raw: str, expected: Any) -> None:
    assert lc._extract_json_array(raw) == expected
