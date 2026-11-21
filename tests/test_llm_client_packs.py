"""Regression tests for auditor/llm_client.py fallback heuristics and line parsing."""
from __future__ import annotations

import pytest

from auditor.contracts import CodeUnit
from auditor.llm_client import _heuristic_findings, _parse_findings_json, _safe_line


def _unit(text: str, start: int = 10) -> CodeUnit:
    return CodeUnit(id="u", repo="r", file_path="r/a.py", symbol="f", kind="function",
                    language="python", start_line=start, end_line=start + text.count("\n"),
                    raw_text=text)


def _kinds(text: str) -> dict[str, int]:
    return {f.title: f.line for f in _heuristic_findings(_unit(text))}


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_safe_line_infinite_falls_back(value):
    assert _safe_line(value, 7) == 7


def test_overflowing_line_in_llm_json_does_not_abort_parsing():
    findings = _parse_findings_json('[{"title": "a", "line": 1e999}, {"title": "b", "line": 3}]', _unit("x"))
    assert [(f.title, f.line) for f in findings] == [("a", 10), ("b", 3)]


@pytest.mark.parametrize("text", [
    "def hash_password(pw):\n    return bcrypt(pw)\n",
    "key = os.environ['SECRET_KEY']\n",
    "pw = os.getenv('DB_PASSWORD', '')\n",
    '"""Rotate the secret."""\n',
    "form = {'password': field}\n",
    "if password == 'abcd':\n    pass\n",
])
def test_secret_rule_ignores_mentions(text):
    assert "Potential hardcoded secret" not in _kinds(text)


@pytest.mark.parametrize("text,line", [
    ("x = 1\nAPI_KEY = 'sk-live-abcdef'\n", 11),
    ("cfg = {\n  'password': \"hunter22\",\n}\n", 11),
    ("token: str = 'ghp_abcdef'\n", 10),
])
def test_secret_rule_flags_literals_with_their_line(text, line):
    findings = [f for f in _heuristic_findings(_unit(text)) if f.title == "Potential hardcoded secret"]
    assert len(findings) == 1
    assert findings[0].line == line and findings[0].severity == "HIGH"
    # Evidence is the cited line with the secret literal masked, never the secret itself.
    cited = text.split("\n")[line - 10].strip()
    head, _, tail = findings[0].evidence.partition("***")
    assert _ and cited.startswith(head) and cited.endswith(tail)


@pytest.mark.parametrize("text", [
    "pprint(x)\n", "fp = fingerprint(x)\n", "bp = blueprint(app)\n", "doc.print(x)\n",
    "todos = []\n", "todo_list.append(x)\n",
])
def test_quality_rules_ignore_substrings(text):
    assert _kinds(text) == {}


def test_quality_rules_report_matching_lines():
    kinds = _kinds("a = 1\n# TODO: fix\nb = 2\nprint(b)\n")
    assert kinds == {"TODO marker found": 11, "Debug print statement present": 13}


def _ollama(endpoint: str, num_gpu_layers: int = -1):
    from auditor.config import ModelProviderConfig
    from auditor.llm_client import LLMClient

    cfg = ModelProviderConfig(provider="ollama", model="m", endpoint=endpoint, temperature=0.1)
    return LLMClient(provider_config=cfg, num_gpu_layers=num_gpu_layers)


@pytest.mark.parametrize("gpu", [-1, 12])
def test_ollama_temperature_goes_in_options(monkeypatch, gpu):
    c = _ollama(f"http://packs-temp-{gpu}/api/generate", num_gpu_layers=gpu)
    seen: dict = {}
    monkeypatch.setattr(c, "_do_generate", lambda payload, headers: seen.update(payload) or "[]")
    assert c.generate("hi") == "[]"
    assert "temperature" not in seen
    assert seen["options"]["temperature"] == 0.1
    assert seen["options"].get("num_gpu") == (gpu if gpu >= 0 else None)


@pytest.mark.parametrize("body", [ValueError("not json"), {"models": ["qwen3:8b"]}, ["x"]])
def test_ollama_health_check_malformed_tags_is_unhealthy(monkeypatch, body):
    import auditor.llm_client as llm_mod

    class Resp:
        def raise_for_status(self) -> None: ...

        def json(self):
            if isinstance(body, Exception):
                raise body
            return body

    monkeypatch.setattr(llm_mod.requests, "get", lambda url, timeout: Resp())
    ok, msg = _ollama("http://packs-health/api/generate").health_check()
    assert ok is False and msg


def test_file_briefing_is_relative_to_the_repo_root(tmp_path):
    root = tmp_path / "mirrors" / "repo"
    unit = CodeUnit(id="u", repo="repo", file_path=str(root / "pkg" / "a.py"), symbol="f",
                    kind="function", language="python", start_line=1, end_line=2, raw_text="x")
    prompt = _ollama("http://packs-brief/api/generate").build_prompt(unit, repo_path=str(root))
    assert "Auditing: pkg/a.py" in prompt
