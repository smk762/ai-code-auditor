"""classify_query matches whole words, not substrings."""

from __future__ import annotations

import pytest

from copilot.query_router import classify_query


@pytest.mark.parametrize(
    "query",
    [
        "explain the shutdown sequence",
        "how does the download handler work",
        "show a breakdown of the scan",
        "is this a known issue",
        "are these steps independent",
    ],
    ids=["shutdown", "download", "breakdown", "known", "independent"],
)
def test_substrings_do_not_misroute(query):
    assert classify_query(query) == "explain"


@pytest.mark.parametrize(
    ("query", "mode"),
    [
        ("who owns the scanner", "ownership"),
        ("which team is the owner of this", "ownership"),
        ("where is the config loaded", "ownership"),
        ("what does auditor depend on", "dependency"),
        ("list the dependencies of wiki", "dependency"),
        ("what breaks if I remove this", "impact"),
    ],
    ids=["who-owns", "owner-of", "where-is", "depend-on", "dependencies", "what-breaks"],
)
def test_real_intents_still_route(query, mode):
    assert classify_query(query) == mode
