"""Tests for the inter-service call-graph analyzer."""
from __future__ import annotations

from pathlib import Path

from ecosystem.call_graph_analyzer import (
    analyze_call_graph,
    detect_call_graph_violations,
    violations_from_edges,
)


_RULES = {
    "service_url_env_vars": [
        {"env_fragment": "GOTHMOG", "service": "gothmog"},
        {"env_fragment": "KIMINI", "service": "kimini-api"},
        {"env_fragment": "IMOGEN", "service": "imogen"},
    ],
    "allowed_calls": [
        {"from": "kimini-api", "to": "gothmog"},
        {"from": "kimini-api", "to": "imogen"},
        {"from": "sauron", "to": "*"},
    ],
}


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_analyze_returns_allowed_and_violation_edges(tmp_path: Path) -> None:
    # kimini-api → gothmog (allowed)
    kimini = tmp_path / "kimini"
    kimini.mkdir()
    f1 = _write(
        kimini / "client.py",
        'import os, httpx\nGOTHMOG_URL = os.getenv("GOTHMOG_URL")\nhttpx.post(GOTHMOG_URL)\n',
    )

    # somnus → imogen (NOT allowed — direct worker bypass)
    somnus = tmp_path / "somnus"
    somnus.mkdir()
    f2 = _write(
        somnus / "proxy.py",
        'import os, requests\nIMOGEN_URL = os.getenv("IMOGEN_URL")\nrequests.get(IMOGEN_URL)\n',
    )

    edges = analyze_call_graph(
        repo_files={"kimini-api": [f1], "somnus": [f2]},
        call_graph_rules=_RULES,
    )

    by_pair = {(e.from_repo, e.to_service): e for e in edges}
    assert by_pair[("kimini-api", "gothmog")].allowed is True
    assert by_pair[("somnus", "imogen")].allowed is False
    assert any("client.py" in ev for ev in by_pair[("kimini-api", "gothmog")].evidence)


def test_analyze_classifies_wildcard_sender_as_allowed(tmp_path: Path) -> None:
    sauron = tmp_path / "sauron"
    sauron.mkdir()
    f = _write(
        sauron / "probe.py",
        'import os, httpx\nKIMINI_URL = os.getenv("KIMINI_URL")\nhttpx.get(KIMINI_URL)\n',
    )

    edges = analyze_call_graph(
        repo_files={"sauron": [f]},
        call_graph_rules=_RULES,
    )

    assert len(edges) == 1
    assert edges[0].from_repo == "sauron"
    assert edges[0].to_service == "kimini-api"
    assert edges[0].allowed is True


def test_violations_from_edges_skips_allowed(tmp_path: Path) -> None:
    somnus = tmp_path / "somnus"
    somnus.mkdir()
    bad = _write(
        somnus / "proxy.py",
        'import os, requests\nIMOGEN_URL = os.getenv("IMOGEN_URL")\nrequests.get(IMOGEN_URL)\n',
    )
    kimini = tmp_path / "kimini"
    kimini.mkdir()
    good = _write(
        kimini / "client.py",
        'import os, httpx\nGOTHMOG_URL = os.getenv("GOTHMOG_URL")\nhttpx.post(GOTHMOG_URL)\n',
    )

    edges = analyze_call_graph(
        repo_files={"somnus": [bad], "kimini-api": [good]},
        call_graph_rules=_RULES,
    )
    violations = violations_from_edges(edges)

    assert len(violations) == 1
    assert violations[0].entities == ["somnus", "imogen"]
    assert violations[0].rule_name == "call_graph_violation"


def test_detect_call_graph_violations_back_compat(tmp_path: Path) -> None:
    """Public legacy API still returns the same RuleViolation shape."""
    somnus = tmp_path / "somnus"
    somnus.mkdir()
    f = _write(
        somnus / "proxy.py",
        'import os, requests\nIMOGEN_URL = os.getenv("IMOGEN_URL")\nrequests.get(IMOGEN_URL)\n',
    )

    violations = detect_call_graph_violations(
        repo_files={"somnus": [f]},
        call_graph_rules=_RULES,
    )

    assert len(violations) == 1
    v = violations[0]
    assert v.rule_name == "call_graph_violation"
    assert v.entities == ["somnus", "imogen"]
    assert v.severity == "HIGH"
    assert "proxy.py" in v.evidence
