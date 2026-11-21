"""conductor.missions.parse_worker_json: the result object survives surrounding braces, and a
malformed ``findings`` field never crashes the parser or turns into characters or keys."""

from __future__ import annotations

import json

import pytest

from conductor.missions import extract_json_object, parse_worker_json


def test_result_is_found_among_quoted_braces() -> None:
    raw = (
        "I looked at `def f(): return {\"a\": 1}`.\n"
        + json.dumps({"status": "COMPLETED", "summary": "ok", "findings": [{"title": "x"}]})
        + "\nPatch: `cfg = {}`"
    )
    r = parse_worker_json(raw, "t")
    assert r.status == "COMPLETED"
    assert r.findings == [{"title": "x"}]


def test_result_in_a_fenced_block() -> None:
    raw = "Notes `{}`\n```json\n" + json.dumps({"status": "PARTIAL", "summary": "s"}) + "\n```"
    r = parse_worker_json(raw, "t")
    assert (r.status, r.summary) == ("PARTIAL", "s")


def test_object_without_status_still_parses() -> None:
    r = parse_worker_json(json.dumps({"summary": "s", "findings": []}), "t")
    assert (r.status, r.summary) == ("PARTIAL", "s")


def test_no_json_and_broken_json_fail() -> None:
    assert parse_worker_json("nothing here", "t").summary == "no JSON"
    assert parse_worker_json("{not json", "t").summary == "invalid JSON"


@pytest.mark.parametrize("value,expected", [
    (5, []),
    (True, []),
    (None, []),
    ("SQL injection in login", ["SQL injection in login"]),
    ({"title": "x", "file": "a.py"}, [{"title": "x", "file": "a.py"}]),
])
def test_non_list_findings_are_normalised(value: object, expected: list) -> None:
    r = parse_worker_json(json.dumps({"status": "COMPLETED", "findings": value}), "t")
    assert r.status == "COMPLETED"
    assert r.findings == expected


def test_extract_prefers_the_object_with_the_key() -> None:
    raw = '{"a": 1} then {"verdict": "clean"}'
    assert extract_json_object(raw, "verdict") == {"verdict": "clean"}
    assert extract_json_object(raw, "missing") == {"a": 1}
    assert extract_json_object("", "x") is None
