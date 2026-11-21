"""Council verdict and classifier label parsing: payloads among stray braces, wrong value types."""
from __future__ import annotations

import pytest

from auditor import finding_class as fc
from auditor import finding_class_council as council
from auditor import finding_class_llm as llm

V = set(fc.taxonomy().ids) | llm.EXTRA_LABELS


@pytest.mark.parametrize("text", [
    'Example shape: {i, verdict}\n{"verdicts": [{"i": 0, "verdict": "agree"}]}',
    '{"verdicts": [{"i": 0, "verdict": "agree"}]}\nNote: {see above}',
    '{"draft": true}\n{"verdicts": [{"i": 0, "verdict": "agree"}]}',
])
def test_verdicts_survive_braces_outside_the_payload(text: str) -> None:
    assert council.parse_verdicts(text, 2, V) == [{"verdict": "agree", "better": "", "why": ""}, None]


@pytest.mark.parametrize("value", ["3", "true", '"agree"', '{"i": 0}', "null"])
def test_non_list_verdicts_degrade_to_none(value: str) -> None:
    assert council.parse_verdicts(f'{{"verdicts": {value}}}', 2, V) == [None, None]


@pytest.mark.parametrize("text", [
    'Format: {i, class}\n{"labels": [{"i": 1, "class": "ssrf"}]}',
    '{"labels": [{"i": 1, "class": "ssrf"}]} and {that} is all',
])
def test_labels_survive_braces_outside_the_payload(text: str) -> None:
    assert llm.parse_labels(text, 2, V) == ["", "ssrf"]


@pytest.mark.parametrize("value", ["3", "true", '{"i": 0}', "null"])
def test_non_list_labels_degrade_to_unclassified(value: str) -> None:
    assert llm.parse_labels(f'{{"labels": {value}}}', 2, V) == ["", ""]
