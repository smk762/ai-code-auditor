"""Regression tests for provider and ecosystem config loading (auditor/config.py)."""
from __future__ import annotations

import pytest

from auditor.config import (
    load_ecosystem_config,
    load_embedder_provider_config,
    load_model_provider_config,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "AI_AUDIT_LLM_PROVIDER", "AI_AUDIT_LLM_MODEL", "AI_AUDIT_LLM_ENDPOINT",
        "AI_AUDIT_EMBEDDER_PROVIDER", "AI_AUDIT_EMBEDDER_MODEL", "AI_AUDIT_EMBEDDER_ENDPOINT",
        "AI_AUDIT_EMBEDDER_DIM",
    ):
        monkeypatch.delenv(name, raising=False)


def test_unknown_llm_provider_is_a_config_error(tmp_path, monkeypatch):
    cfg = tmp_path / "generator.yaml"
    cfg.write_text("provider: anthropic\nproviders:\n  anthropic:\n    model: m\n    endpoint: e\n")
    monkeypatch.setenv("AI_AUDIT_LLM_PROVIDER", "anthropc")
    with pytest.raises(ValueError, match="anthropc"):
        load_model_provider_config(cfg)


def test_unknown_embedder_provider_is_a_config_error(tmp_path, monkeypatch):
    cfg = tmp_path / "embedder.yaml"
    cfg.write_text("provider: local-hash\nproviders:\n  local-hash:\n    model: local-hash\n")
    monkeypatch.setenv("AI_AUDIT_EMBEDDER_PROVIDER", "olama")
    with pytest.raises(ValueError, match="olama"):
        load_embedder_provider_config(cfg)


def test_default_providers_still_work_without_a_block(tmp_path):
    missing = tmp_path / "absent.yaml"
    llm = load_model_provider_config(missing)
    assert llm.provider == "ollama" and "11434" in llm.endpoint
    emb = load_embedder_provider_config(missing)
    assert emb.provider == "local-hash" and emb.model == "local-hash"


def test_empty_providers_key_does_not_crash(tmp_path):
    cfg = tmp_path / "embedder.yaml"
    cfg.write_text("provider: local-hash\nproviders:\n")
    assert load_embedder_provider_config(cfg).model == "local-hash"
    gen = tmp_path / "generator.yaml"
    gen.write_text("provider: ollama\nproviders:\n")
    assert load_model_provider_config(gen).provider == "ollama"


def test_all_repos_disabled_is_rejected(tmp_path):
    cfg = tmp_path / "ecosystem.yaml"
    cfg.write_text("repos:\n  - name: a\n    path: /x\n    enabled: false\n")
    with pytest.raises(ValueError, match="enabled"):
        load_ecosystem_config(cfg)
