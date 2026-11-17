"""Prompt caching on the paid paths: scan prompts carry breakpoints without changing their text,
batches use 1-hour entries grouped by repo, and the ledger prices what the API reports."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from auditor.config import ModelProviderConfig, RepoConfig
from auditor.contracts import CodeUnit
from auditor.llm_client import LLMClient, prompt_blocks
from auditor.spend_ledger import SpendLedger, cost_usd


def _unit(repo: str = "r", path: str = "r/a.py", sym: str = "f", text: str = "def f():\n    return 1\n") -> CodeUnit:
    return CodeUnit(id=f"{repo}:{path}:{sym}", repo=repo, file_path=path, symbol=sym, kind="function",
                    language="python", start_line=1, end_line=2, raw_text=text)


def _client(briefing: str = "## Repo Context\n**r** — svc") -> LLMClient:
    return LLMClient(provider_config=ModelProviderConfig(provider="anthropic", model="claude-haiku-5-5",
                                                         endpoint=""), repo_briefing=briefing)


@pytest.fixture
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "spend.db"
    monkeypatch.setenv("AI_AUDIT_SPEND_DB", str(path))
    for k in ("AI_AUDIT_BUDGET_TOTAL_USD", "AI_AUDIT_BUDGET_AUDIT_USD", "AI_AUDIT_BUDGET_AUDIT_BATCH_USD"):
        monkeypatch.delenv(k, raising=False)
    return path


@pytest.mark.parametrize("briefing", ["## Repo Context\n**r** — svc", ""])
def test_blocks_are_the_same_prompt_with_breakpoints_on_the_shared_prefix(briefing: str) -> None:
    c, unit = _client(briefing), _unit()
    blocks = c.build_prompt_blocks(unit, "/tmp/r")
    assert "".join(b["text"] for b in blocks) == c.build_prompt(unit, "/tmp/r")
    assert all(b["text"].strip() for b in blocks)  # the API rejects whitespace-only blocks
    cached = [b for b in blocks if "cache_control" in b]
    assert cached and len(cached) == (2 if briefing else 1)
    assert cached[0]["text"].startswith(c._ecosystem_overview)
    assert "cache_control" not in blocks[-1] and unit.raw_text in blocks[-1]["text"]
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in cached)
    assert all(b["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
               for b in c.build_prompt_blocks(unit, "/tmp/r", ttl="1h") if "cache_control" in b)


def test_whitespace_segment_first_in_line_folds_forward() -> None:
    blocks = prompt_blocks([("\n", True), ("a", True), ("b", False)])
    assert [b["text"] for b in blocks] == ["\na", "b"] and "cache_control" in blocks[0]


def test_sync_scan_sends_blocks_and_records_cache_usage(ledger_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "0")  # the review cache is on by default; keep it out
    monkeypatch.setenv("AI_AUDIT_LLM_EFFORT", "low")
    sent: dict[str, Any] = {}
    usage = SimpleNamespace(input_tokens=300, output_tokens=50, cache_read_input_tokens=700,
                            cache_creation_input_tokens=0, cache_creation=None)

    def create(**kw: Any) -> Any:
        sent.update(kw)
        return SimpleNamespace(usage=usage, stop_reason="end_turn", model=kw["model"],
                               content=[SimpleNamespace(type="text", text="[]")])

    c = _client()
    c._anthropic = SimpleNamespace(messages=SimpleNamespace(create=create))
    assert c.analyze_code(_unit(), "/tmp/r") == []
    assert "temperature" not in sent and sent["output_config"] == {"effort": "low"}
    content = sent["messages"][0]["content"]
    assert isinstance(content, list) and "cache_control" in content[0]
    row = SpendLedger(ledger_env)._conn.execute("SELECT lane, cache_read FROM spend").fetchone()
    assert row == ("audit", 700)


def test_refusal_is_a_failure_not_a_clean_unit(ledger_env: Path) -> None:
    usage = SimpleNamespace(input_tokens=1, output_tokens=1)
    c = _client()
    c._anthropic = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
        usage=usage, stop_reason="refusal", stop_details=None, content=[])))
    assert c.generate("hi") == "" and c._failures == 1


def test_cache_pricing_by_ttl_and_model() -> None:
    m = 1_000_000
    assert cost_usd("claude-opus-5-5", 0, 0, cache_write=m) == pytest.approx(5.0)          # 1.25x
    assert cost_usd("claude-opus-5-5", 0, 0, cache_write=m, cache_write_1h=m) == pytest.approx(8.0)  # 2x
    assert cost_usd("claude-opus-5-5", 0, 0, cache_read=m) == pytest.approx(0.20)           # 0.05x
    assert cost_usd("claude-sonnet-5-5", 0, 0, cache_read=m) == pytest.approx(0.10)
    assert cost_usd("claude-haiku-5-5", 0, 0, cache_read=m) == pytest.approx(0.01)          # 0.1x
    assert cost_usd("claude-opus-5-5", 0, 0, cache_write=m, cache_write_1h=m, batch=True) == pytest.approx(4.0)


def test_record_usage_reads_the_1h_write_split(ledger_env: Path) -> None:
    led = SpendLedger(ledger_env)
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000,
             "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000, "ephemeral_5m_input_tokens": 0}}
    assert led.record_usage("wiki", "claude-opus-5-5", usage) == pytest.approx(8.0)
    obj = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                          cache_creation_input_tokens=1_000_000,
                          cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=0))
    assert led.record_usage("wiki", "claude-opus-5-5", obj) == pytest.approx(5.0)


def test_batch_prefetch_submits_1h_breakpoints_grouped_by_repo(
    ledger_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic

    import auditor.batch_review as br

    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "1")
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("AI_AUDIT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("AI_AUDIT_LLM_MODEL", "claude-haiku-5-5")
    # Risky units in both repos, so the budget-fit order interleaves the repos.
    units = {
        r: [_unit(r, f"{r}/{f}.py", s, f"# {r}/{f}/{s}\n"
                  + ("def check_auth(token):\n" if s == "risky" else "def f():\n") + "  x\n" * n)
            for f, s, n in (("b", "risky", 40), ("a", "plain", 5), ("a", "risky", 10))]
        for r in ("r1", "r2")
    }
    monkeypatch.setattr(br, "resolve_scan_root", lambda repo: Path("/tmp") / repo.name)
    monkeypatch.setattr(br, "scan_repo", lambda repo: [repo.name])
    monkeypatch.setattr(br, "extract_code_units", lambda name, f: units[name])
    monkeypatch.setattr(br, "build_repo_briefing", lambda name, root: f"## Repo Context\n**{name}**")
    submitted: list[dict[str, Any]] = []

    class Batches:
        def create(self, requests: list[dict[str, Any]]) -> Any:
            submitted.extend(requests)
            return SimpleNamespace(id="b1")

        def retrieve(self, batch_id: str) -> Any:
            return SimpleNamespace(processing_status="ended")

        def results(self, batch_id: str) -> list[Any]:
            return []

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(
        messages=SimpleNamespace(batches=Batches())))
    br.prefetch_reviews([RepoConfig(name="r1", path="/tmp/r1"), RepoConfig(name="r2", path="/tmp/r2")])

    every = [u for r in units.values() for u in r]
    order = [next((u.repo, u.file_path) for u in every
                  if f"# {u.repo}/{Path(u.file_path).stem}/{u.symbol}\n" in req["params"]["messages"][0]["content"][-1]["text"])
             for req in submitted]
    assert order == sorted(order) and len(order) == 6
    for req in submitted:
        cached = [b for b in req["params"]["messages"][0]["content"] if "cache_control" in b]
        assert len(cached) == 2 and all(b["cache_control"]["ttl"] == "1h" for b in cached)


def test_wiki_request_id_ignores_the_cache_ttl_only() -> None:
    from wiki.batch import request_id

    def params(ttl: str | None, text: str = "ctx") -> dict[str, Any]:
        cc = {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
        return {"model": "m", "system": [{"type": "text", "text": text, "cache_control": cc}], "messages": []}

    assert request_id("p", params(None)) == request_id("p", params("1h"))
    assert request_id("p", params("1h")) != request_id("p", params("1h", "other"))
