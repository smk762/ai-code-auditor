"""load_ecosystem_config: a bare ``key:`` (YAML None) takes the default, never None or "None"."""
from __future__ import annotations

import os

import pytest

_KEYS = ("output_dir", "graph_db_path", "embedding_index_path", "architecture_memory_path",
         "auth_code_env", "log_level", "log_file_path", "rag_collection", "rag_ingest_url")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AI_AUDIT_OUTPUT_DIR", "AI_AUDIT_GRAPH_DB_PATH", "AI_AUDIT_EMBEDDING_INDEX_PATH",
              "AI_AUDIT_ARCH_MEMORY_PATH", "AI_AUDIT_AUTH_CODE_ENV", "AI_AUDIT_LOG_LEVEL",
              "AI_AUDIT_LOG_FILE_PATH", "AI_AUDIT_RAG_COLLECTION", "AI_AUDIT_RAG_INGEST_URL",
              "AI_AUDIT_AUTH_MODE"):
        monkeypatch.delenv(k, raising=False)


def test_bare_keys_take_the_defaults(tmp_path) -> None:
    from auditor.config import EcosystemConfig, RepoConfig, load_ecosystem_config

    p = tmp_path / "ecosystem.yaml"
    p.write_text("repos:\n  - name: a\n    path: /a\n    branch:\n    provider:\n    local_cache_path:\n"
                 "    access_token_env:\n" + "".join(f"{k}:\n" for k in _KEYS))
    cfg = load_ecosystem_config(p)
    want = EcosystemConfig(repos=[])
    for k in _KEYS:
        assert getattr(cfg, k) == getattr(want, k), k
    r, d = cfg.repos[0], RepoConfig(name="a", path="/a")
    assert (r.branch, r.provider, r.local_cache_path, r.access_token_env) == (
        d.branch, d.provider, d.local_cache_path, d.access_token_env)
    assert os.path.basename(cfg.log_file_path) != "None"
