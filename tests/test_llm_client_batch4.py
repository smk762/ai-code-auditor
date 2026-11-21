"""llm_client: array extraction, secret redaction, prompt-block breakpoints, null content."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import auditor.llm_client as lc
from auditor.config import ModelProviderConfig
from auditor.contracts import CodeUnit


def _unit(text: str) -> CodeUnit:
    return CodeUnit(id="u", repo="r", file_path="r/a.py", symbol="f", kind="function",
                    language="python", start_line=10, end_line=12, raw_text=text)


@pytest.mark.parametrize("raw,expected", [
    ('{"findings": [{"title": "x"}]}', [{"title": "x"}]),
    ('[{"title": "x"}]', [{"title": "x"}]),
    ('{"note": "nothing"}', None),
    ("42", None),
])
def test_extract_json_array_only_returns_arrays(raw: str, expected: Any) -> None:
    assert lc._extract_json_array(raw) == expected


def test_wrapped_findings_are_parsed_not_dropped() -> None:
    out = lc._parse_findings_json('{"findings": [{"title": "Bug", "severity": "high"}]}', _unit("x = 1\n"))
    assert out is not None and [f.title for f in out] == ["Bug"]


def test_hardcoded_secret_evidence_is_redacted() -> None:
    secret = "sk-live-1234567890abcdef"
    unit = _unit(f'def f():\n    API_KEY = "{secret}"\n    return API_KEY\n')
    found = [f for f in lc._heuristic_findings(unit) if f.title == "Potential hardcoded secret"]
    assert len(found) == 1
    assert secret not in found[0].evidence and found[0].evidence == 'API_KEY = "***"'
    assert found[0].line == 11


def test_whitespace_segment_keeps_its_breakpoint_on_the_block_before() -> None:
    blocks = lc.prompt_blocks([("a", False), ("\n\n", True), ("b", False)])
    assert [b["text"] for b in blocks] == ["a\n\n", "b"]
    assert blocks[0].get("cache_control") == {"type": "ephemeral"} and "cache_control" not in blocks[1]


def test_all_whitespace_segments_give_no_blocks() -> None:
    assert lc.prompt_blocks([("\n", True), ("  ", False)]) == []


def test_null_openai_content_is_an_empty_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    c = lc.LLMClient(provider_config=ModelProviderConfig(provider="openai-compatible", model="m",
                                                         endpoint="http://x/v1/chat/completions"))
    resp = SimpleNamespace(raise_for_status=lambda: None,
                           json=lambda: {"choices": [{"message": {"role": "assistant", "content": None}}]})
    monkeypatch.setattr(lc.requests, "post", lambda *a, **k: resp)
    assert c._do_generate({}, {}) == ""
