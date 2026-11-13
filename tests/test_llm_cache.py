from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import auditor.batch_review as br
from auditor.config import ModelProviderConfig, RepoConfig
from auditor.contracts import CodeUnit
from auditor.llm_cache import ReviewCache
from auditor.llm_client import LLMClient


def _unit(text: str = "def f():\n    return 1\n", uid: str = "u1") -> CodeUnit:
    return CodeUnit(id=uid, repo="r", file_path="r/a.py", symbol=uid, kind="function",
                    language="python", start_line=1, end_line=2, raw_text=text)


@pytest.fixture
def cache_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cache.db"
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "1")
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE_PATH", str(path))
    return path


def _client(model: str = "claude-haiku-5-5") -> LLMClient:
    return LLMClient(provider_config=ModelProviderConfig(provider="anthropic", model=model, endpoint=""))


def test_cache_roundtrip_and_ttl(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "c.db", ttl_days=1)
    c.put("k", "m", "[]")
    assert c.get("k") == "[]"
    c._conn.execute("UPDATE reviews SET created_at = ?", (time.time() - 2 * 86400,))
    assert c.get("k") is None


def test_unchanged_unit_is_not_re_reviewed(cache_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    calls: list[str] = []
    monkeypatch.setattr(c, "generate", lambda p: calls.append(p) or '[{"title": "t"}]')
    first = c.analyze_code(_unit())
    second = c.analyze_code(_unit())
    assert len(calls) == 1 and c.cache_hits == 1
    assert [f.title for f in first] == [f.title for f in second] == ["t"]
    # Changed code → new review.
    c.analyze_code(_unit("def f():\n    return 2\n"))
    assert len(calls) == 2


def test_unparseable_response_is_not_cached(cache_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    calls: list[str] = []
    monkeypatch.setattr(c, "generate", lambda p: calls.append(p) or "sorry, no json")
    c.analyze_code(_unit())
    c.analyze_code(_unit())
    assert len(calls) == 2


def test_key_depends_on_model_and_effort(cache_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    u = _unit()
    k_haiku = _client("claude-haiku-5-5").cache_key(u)
    assert _client("claude-sonnet-5-5").cache_key(u) != k_haiku
    monkeypatch.setenv("AI_AUDIT_LLM_EFFORT", "medium")
    assert _client("claude-haiku-5-5").cache_key(u) != k_haiku


class _FakeBatches:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.created: list[list[dict[str, Any]]] = []

    def create(self, requests: list[dict[str, Any]]) -> Any:
        self.created.append(requests)
        return SimpleNamespace(id=f"batch_{len(self.created)}")

    def retrieve(self, batch_id: str) -> Any:
        return SimpleNamespace(processing_status="ended", request_counts=SimpleNamespace(processing=0))

    def results(self, batch_id: str) -> Any:
        reqs = self.created[int(batch_id.split("_")[1]) - 1]
        for r in reqs:
            msg = SimpleNamespace(
                content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=self.reply)],
                usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
            )
            yield SimpleNamespace(custom_id=r["custom_id"],
                                  result=SimpleNamespace(type="succeeded", message=msg))


def test_batch_prefetch_fills_cache_then_skips(cache_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("AI_AUDIT_LLM_MODEL", "claude-haiku-5-5")
    units = [_unit(uid="a"), _unit("def g():\n    pass\n", uid="b")]
    monkeypatch.setattr(br, "resolve_scan_root", lambda repo: Path("/tmp"))
    monkeypatch.setattr(br, "scan_repo", lambda repo: ["/tmp/a.py"])
    monkeypatch.setattr(br, "extract_code_units", lambda name, f: units)
    monkeypatch.setattr(br, "build_repo_briefing", lambda name, root: "")
    fake = _FakeBatches('[{"title": "batched"}]')
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(
        messages=SimpleNamespace(batches=fake)))
    repos = [RepoConfig(name="r", path="/tmp")]

    stats = br.prefetch_reviews(repos, poll_s=0)
    assert stats.submitted == 2 and stats.cached_new == 2
    req = fake.created[0][0]
    assert req["params"]["output_config"] == {"effort": "low"}
    assert len(req["custom_id"]) <= 64
    assert stats.cost_usd == pytest.approx(2 * (1000 * 0.10 + 100 * 0.50) / 1e6 * 0.5)

    # Second run: everything cached, nothing submitted; normal pass reads the cache.
    stats2 = br.prefetch_reviews(repos, poll_s=0)
    assert stats2.submitted == 0 and stats2.already_cached == 2
    c = _client()
    monkeypatch.setattr(c, "generate", lambda p: pytest.fail("should be a cache hit"))
    assert [f.title for f in c.analyze_code(units[0])] == ["batched"]
