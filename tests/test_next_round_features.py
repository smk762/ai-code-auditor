from __future__ import annotations
from pathlib import Path

import pytest

from auditor.config import load_embedder_provider_config, load_model_provider_config, RepoConfig
from auditor.repo_resolver import resolve_repo_path
from auditor.runtime import require_auth
from copilot.copilot_engine import CopilotEngine
from ecosystem.graph_builder import GraphBuilder
from semantic.embedding_index import EmbeddingIndex
from semantic.semantic_search import SemanticSearch


def test_provider_configs_default_when_files_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model_cfg = load_model_provider_config()
    embed_cfg = load_embedder_provider_config()
    assert model_cfg.provider == "ollama"
    assert embed_cfg.provider == "local-hash"


def test_remote_repo_resolution_uses_cache_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((cmd, kwargs))
        class Result:
            returncode = 0
            stderr = ""
        return Result()

    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token")
    monkeypatch.setattr("subprocess.run", fake_run)

    repo = RepoConfig(
        name="sample",
        path="https://github.com/org/repo.git",
        branch="main",
        access_token_env="GITHUB_TOKEN",
        local_cache_path=str(tmp_path / "cache"),
    )
    resolved = resolve_repo_path(repo)
    assert resolved == tmp_path / "cache" / "sample"
    assert calls, "expected git clone/fetch command call"
    joined = " ".join(calls[0][0])
    assert "dummy-token" not in joined
    assert "env" in calls[0][1]


def test_deep_research_response_shape(tmp_path: Path) -> None:
    index = EmbeddingIndex(path=str(tmp_path / "idx.faiss"))
    search = SemanticSearch(index)
    graph = GraphBuilder(str(tmp_path / "graph.db"))
    try:
        engine = CopilotEngine(search=search, graph=graph)
        result = engine.deep_research("What breaks if I change auth?", max_iterations=2)
        assert "plan" in result
        assert "updates" in result
        assert len(result["updates"]) == 2
    finally:
        graph.close()


def test_require_auth_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_AUTH_CODE", "secret")
    require_auth(True, "AI_AUDIT_AUTH_CODE", "secret")
    with pytest.raises(PermissionError):
        require_auth(True, "AI_AUDIT_AUTH_CODE", "wrong")
