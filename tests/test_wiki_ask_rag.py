from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auditor.config import ModelProviderConfig, RepoConfig
from auditor.spend_ledger import get_ledger
from tests.test_wiki import APP
from wiki import rag

PAGE = """# svc overview

Intro line [app.py:L9-L11].

## Setup
Install it.

## Setup
Again, a second section with the same heading.

## Interfaces
`create_app` builds the app [app.py:L9-L11].
"""


@pytest.fixture
def built(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "mirrors" / "svc"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text(APP)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "i"], check=True, env=env)
    out = tmp_path / "reports"
    d = out / "wiki" / "svc"
    d.mkdir(parents=True)
    (d / "overview.md").write_text(PAGE)
    (d / "interfaces.md").write_text("# Interfaces\n\nCLI.\n")
    (d / "findings.md").write_text("# Notable findings\n\nsecret-ish\n")
    (d / "INDEX.json").write_text(json.dumps({"repo": "svc", "source_sha": "abc", "pages": [
        {"slug": "overview", "title": "Overview", "file": "overview.md"},
        {"slug": "interfaces", "title": "Interfaces and entry points", "file": "interfaces.md"},
        {"slug": "findings", "title": "Notable findings", "file": "findings.md"}]}))

    class _Cfg:
        repos = [RepoConfig("svc", str(repo))]
        output_dir = str(out)
        auth_mode = False
        auth_code_env = "AI_AUDIT_AUTH_CODE"

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(out))
    monkeypatch.setenv("AI_AUDIT_PUBLIC_BASE_URL", "http://audit.example")
    return out, repo


# ── RAG ingest ───────────────────────────────────────────────────────────────

def test_sections_split_on_h2() -> None:
    s = rag.sections(PAGE)
    assert [h for h, _ in s] == ["Introduction", "Setup", "Setup", "Interfaces"]
    assert s[3][1].startswith("## Interfaces") and "[app.py:L9-L11]" in s[3][1]


def test_documents_for_repo(built) -> None:
    out, _ = built
    docs = rag.documents_for_repo(out, "svc", base_url="http://audit.example/")
    ids = [d["id"] for d in docs]
    assert ids == ["wiki::svc::overview::introduction", "wiki::svc::overview::setup", "wiki::svc::overview::setup-1",
                   "wiki::svc::overview::interfaces", "wiki::svc::interfaces::introduction"]  # never findings
    assert docs[3]["title"] == "svc — Overview — Interfaces"
    assert docs[3]["url"] == "http://audit.example/ui/wiki/svc/overview#interfaces"
    assert docs[3]["text"].startswith("# svc: Overview\n\n## Interfaces")
    assert [d["id"] for d in rag.documents_for_repo(out, "svc", {"interfaces"})] == ["wiki::svc::interfaces::introduction"]


def test_ingest_posts_signed_docs_and_respects_switches(built, monkeypatch) -> None:
    out, _ = built
    posted = []

    class _Resp:
        status_code = 200
        text = "{}"

    import requests

    monkeypatch.setattr(requests, "post", lambda url, data, headers, timeout: posted.append(
        (json.loads(data), headers)) or _Resp())
    monkeypatch.setenv("INGEST_SHARED_SECRET", "s3cret")
    res = rag.ingest(out)
    assert res["ingested"] and res["count"] == 5 and res["repos"]["svc"]["count"] == 5
    body, headers = posted[0]
    assert body["tags"] == ["kind:wiki", "repo:svc"] and len(body["docs"]) == 5
    assert {"X-Timestamp", "X-Nonce", "X-Signature"} <= set(headers)

    monkeypatch.setenv("AI_AUDIT_WIKI_RAG_INGEST", "0")
    assert rag.ingest(out) == {"skipped": "AI_AUDIT_WIKI_RAG_INGEST=0"}
    monkeypatch.delenv("AI_AUDIT_WIKI_RAG_INGEST")
    monkeypatch.delenv("INGEST_SHARED_SECRET")
    res = rag.ingest(out)
    assert res["ingested"] is False and "INGEST_SHARED_SECRET" in res["error"] and len(posted) == 1


# ── Ask this repo ────────────────────────────────────────────────────────────

class FakeClaude:
    def __init__(self, text: str, stop: str = "end_turn"):
        self.text, self.stop, self.calls = text, stop, []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params):
        self.calls.append(params)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason=self.stop,
                               model=params["model"], usage=SimpleNamespace(
                                   input_tokens=20_000, output_tokens=300,
                                   cache_creation_input_tokens=0, cache_read_input_tokens=0))


@pytest.fixture
def claude(monkeypatch) -> None:
    monkeypatch.setattr("auditor.config.load_model_provider_config",
                        lambda *a, **k: ModelProviderConfig(provider="anthropic", model="claude-haiku-5-5", endpoint=""))


def test_ask_answers_with_checked_citations(built, claude) -> None:
    from wiki.ask import ask

    out, _ = built
    fake = FakeClaude("`create_app` builds it [app.py:L9-L11]. It also reads `ghost` [app.py:L1-L2].")
    res = ask(out, "svc", "How is the app built?", client=fake)
    assert "create_app" in res["answer_md"] and "ghost" not in res["answer_md"]
    assert [d["reason"] for d in res["dropped_claims"]] == ["symbol"]
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "## Wiki pages (current)" in call["system"][1]["text"] and "# svc overview" in call["system"][1]["text"]
    assert call["messages"] == [{"role": "user", "content": "How is the app built?"}]
    assert res["cost_usd"] > 0 and get_ledger().spent("ask") == pytest.approx(res["cost_usd"], abs=1e-5)


def test_ask_rejects_bad_input_and_handles_refusal(built, claude) -> None:
    from wiki.ask import ask

    out, _ = built
    with pytest.raises(ValueError):
        ask(out, "svc", "   ", client=FakeClaude("x"))
    with pytest.raises(ValueError):
        ask(out, "svc", "x" * 2001, client=FakeClaude("x"))
    with pytest.raises(LookupError):
        ask(out, "nope", "q", client=FakeClaude("x"))
    res = ask(out, "svc", "q", client=FakeClaude("", stop="refusal"))
    assert res["error"].startswith("The model declined") and res["answer_md"] == ""


def test_ask_endpoint(client: TestClient, built, claude, monkeypatch) -> None:
    out, _ = built
    monkeypatch.setattr("wiki.ask._anthropic", lambda model, effort, ctx, q, client=None: (
        "Built by `create_app` [app.py:L9-L11].", "end_turn", 0.002))
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "tok")
    assert client.post("/audit/wiki/svc/ask", json={"question": "how?"}).status_code == 401
    h = {"X-API-Token": "tok"}
    r = client.post("/audit/wiki/svc/ask", json={"question": "how?"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "create_app" in body["answer_html"] and body["source_sha"]
    assert client.post("/audit/wiki/nope/ask", json={"question": "q"}, headers=h).status_code == 404
    assert client.post("/audit/wiki/svc/ask", json={"question": ""}, headers=h).status_code == 422


def test_ask_with_other_providers_books_the_ask_lane_without_touching_env(built, monkeypatch) -> None:
    """The lane goes to the client, never into os.environ (audit subprocesses inherit that)."""
    import os

    from wiki.ask import ask

    monkeypatch.delenv("AI_AUDIT_SPEND_LANE", raising=False)
    monkeypatch.setattr("auditor.config.load_model_provider_config",
                        lambda *a, **k: ModelProviderConfig(provider="ollama", model="qwen", endpoint="http://x"))
    lanes = []

    def generate(self, prompt):
        lanes.append(self.lane)
        return "`create_app` builds it [app.py:L9-L11]."

    monkeypatch.setattr("auditor.llm_client.LLMClient.generate", generate)
    monkeypatch.setattr("auditor.llm_client.build_ecosystem_overview", lambda: "")
    res = ask(built[0], "svc", "How is the app built?")
    assert "create_app" in res["answer_md"] and lanes == ["ask"]
    assert "AI_AUDIT_SPEND_LANE" not in os.environ
