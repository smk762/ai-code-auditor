from __future__ import annotations

import textwrap
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


# ── TTL jitter and format-only changes (#cost) ─────────────────────────────────

def test_ttl_jitter_spreads_expiry_but_is_fixed_per_key(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "c.db", ttl_days=60, ttl_jitter=0.25)
    ttls = [c.ttl_for(f"k{i}") for i in range(400)]
    days = [t / 86400 for t in ttls]
    assert 45 <= min(days) < 48 and 72 < max(days) <= 75
    assert c.ttl_for("k1") == c.ttl_for("k1")
    # A night's worth of entries written together expires over weeks, not on one night.
    assert len({int(d) for d in days}) >= 25
    # Expiry honours the key's own TTL.
    short = min(range(400), key=lambda i: ttls[i])
    key = f"k{short}"
    c.put(key, "m", "[]")
    c._conn.execute("UPDATE reviews SET created_at = ?", (time.time() - ttls[short] - 60,))
    assert c.get(key) is None
    c._conn.execute("UPDATE reviews SET created_at = ?", (time.time() - ttls[short] + 3600,))
    assert c.get(key) == "[]"


def test_no_jitter_keeps_the_plain_ttl(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "c.db", ttl_days=1)
    assert c.ttl_for("anything") == 86400
    assert ReviewCache(tmp_path / "d.db", ttl_days=0).ttl_for("k") is None


PY = 'def f(a, b):\n    if a:\n        return call(a, b)  # why\n    return "x  y"\n'


@pytest.mark.parametrize("variant", [
    PY.replace("    ", "  "),                                            # re-indent
    PY.replace("\n", "\r\n"),                                            # line endings
    PY.replace("\n", "   \n"),                                           # trailing whitespace
    PY.replace("    return \"", "\n    return \""),                     # blank line
    PY.replace("call(a, b)", "call(\n            a,\n            b\n        )"),  # re-wrapped
    PY.replace("call(a, b)", "call( a ,b )"),                            # spacing
    textwrap.indent(PY, "    "),                                         # nested one level
])
def test_format_only_python_change_keeps_the_key(variant: str) -> None:
    c = _client()
    assert c.cache_key(_unit(variant)) == c.cache_key(_unit(PY))


@pytest.mark.parametrize("variant", [
    PY.replace("return call(a, b)", "return call(b, a)"),
    PY.replace('"x  y"', '"x y"'),                                       # string contents
    PY.replace("# why", "# TODO: unsafe"),                               # comments count
    PY.replace("        return call", "    return call"),                # dedent changes meaning
])
def test_real_python_change_gets_a_new_key(variant: str) -> None:
    c = _client()
    assert c.cache_key(_unit(variant)) != c.cache_key(_unit(PY))


def test_python_normalisation_is_pinned() -> None:
    """Cached reviews are keyed on this exact text: changing it re-reviews every unit."""
    from auditor.llm_cache import normalise_code

    assert normalise_code(PY, "python") == (
        'def f ( a , b ) : <nl> <indent> if a : <nl> <indent> return call ( a , b ) # why <nl> '
        '<dedent> return "x  y" <nl> <dedent>'
    )


def test_other_languages_ignore_whitespace_only() -> None:
    def ts(text: str) -> CodeUnit:
        return CodeUnit(id="t", repo="r", file_path="r/a.ts", symbol="a.ts", kind="file",
                        language="ts", start_line=1, end_line=3, raw_text=text)
    c = _client()
    js = "export function f(a) {\n  return a + 1;\n}\n"
    assert c.cache_key(ts(js)) == c.cache_key(ts("export function f(a)  {\n\n\treturn a + 1;  \n}"))
    assert c.cache_key(ts(js)) != c.cache_key(ts(js.replace("+ 1", "+ 2")))


def test_unparseable_python_falls_back_to_line_normalising() -> None:
    c = _client()
    broken = 'def f(:\n    x = """unterminated\n'
    assert c.cache_key(_unit(broken)) == c.cache_key(_unit(broken.replace("\n", "  \n")))


def test_reviews_under_the_old_key_are_reused_without_a_rescan(
        cache_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from auditor.llm_cache import legacy_review_key

    c = _client()
    u = _unit()
    old = legacy_review_key(provider=c.provider, model=c.model, effort=c.effort,
                            instructions=c.instructions, unit=u)
    assert old != c.cache_key(u)
    written = time.time() - 10 * 86400
    c.cache.put(old, c.model, '[{"title": "old"}]')
    c.cache._conn.execute("UPDATE reviews SET created_at = ?", (written,))
    monkeypatch.setattr(c, "generate", lambda p: pytest.fail("should reuse the legacy review"))
    assert [f.title for f in c.analyze_code(u)] == ["old"]
    # Copied under the new key so it still expires when the legacy entry would have (each
    # key's TTL is jittered differently, so created_at is shifted by the difference).
    row = c.cache._conn.execute("SELECT created_at FROM reviews WHERE key = ?",
                                (c.cache_key(u),)).fetchone()
    assert row and row[0] + c.cache.ttl_for(c.cache_key(u)) == pytest.approx(
        written + c.cache.ttl_for(old))
    # The batch prefetch sees it as cached too.
    assert c.cache.has(c.cache_key(u), *c.legacy_cache_keys(u))
