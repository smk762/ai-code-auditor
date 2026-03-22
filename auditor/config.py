from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml

from auditor.env_loader import load_env_file

load_env_file()


@dataclass(slots=True)
class RepoConfig:
    name: str
    path: str
    branch: str = ""  # empty → auto-detect (dev → main → master)
    enabled: bool = True
    access_token_env: str = ""
    provider: str = "local"
    local_cache_path: str = ""
    # Optional directory inside the repo root to scan (local path or post-clone). Empty = whole repo.
    subpath: str = ""


@dataclass(slots=True)
class EcosystemConfig:
    repos: list[RepoConfig]
    output_dir: str = "reports"
    graph_db_path: str = "graph/ecosystem_graph.db"
    embedding_index_path: str = "index/code_embeddings.faiss"
    architecture_memory_path: str = "memory/architecture_memory.md"
    auth_mode: bool = False
    auth_code_env: str = "AI_AUDIT_AUTH_CODE"
    log_level: str = "INFO"
    log_file_path: str = ""
    # Isolated Qdrant collection for audit knowledge — separate from the chat RAG.
    rag_collection: str = "audit_docs"
    rag_ingest_url: str = "http://192.168.1.128:9050/ingest"


@dataclass(slots=True)
class ModelProviderConfig:
    provider: str
    model: str
    endpoint: str
    timeout_s: int = 300      # inference timeout; large local models need time
    healthcheck_timeout_s: int = 10  # fast probe — connection refused or model-not-found, not a full generate
    api_key_env: str = ""
    temperature: float = 0.1


@dataclass(slots=True)
class EmbedderProviderConfig:
    provider: str
    model: str
    dim: int = 64
    endpoint: str = ""
    api_key_env: str = ""


def _read_yaml(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_ecosystem_config(path: str | Path = "config/ecosystem.yaml") -> EcosystemConfig:
    raw = _read_yaml(path)
    repos = [
        RepoConfig(
            name=item["name"],
            path=item["path"],
            branch=item.get("branch", ""),
            enabled=item.get("enabled", True),
            access_token_env=item.get("access_token_env", ""),
            provider=item.get("provider", "local"),
            local_cache_path=item.get("local_cache_path", ""),
            subpath=str(item.get("subpath", "") or ""),
        )
        for item in raw.get("repos", [])
    ]
    if not repos:
        raise ValueError("config/ecosystem.yaml must define at least one repository in repos[].")
    return EcosystemConfig(
        repos=[r for r in repos if r.enabled],
        output_dir=os.getenv("AI_AUDIT_OUTPUT_DIR", raw.get("output_dir", "reports")),
        graph_db_path=os.getenv("AI_AUDIT_GRAPH_DB_PATH", raw.get("graph_db_path", "graph/ecosystem_graph.db")),
        embedding_index_path=os.getenv("AI_AUDIT_EMBEDDING_INDEX_PATH", raw.get("embedding_index_path", "index/code_embeddings.faiss")),
        architecture_memory_path=os.getenv(
            "AI_AUDIT_ARCH_MEMORY_PATH",
            raw.get("architecture_memory_path", "memory/architecture_memory.md"),
        ),
        auth_mode=_bool_env("AI_AUDIT_AUTH_MODE", bool(raw.get("auth_mode", False))),
        auth_code_env=os.getenv("AI_AUDIT_AUTH_CODE_ENV", raw.get("auth_code_env", "AI_AUDIT_AUTH_CODE")),
        log_level=str(os.getenv("AI_AUDIT_LOG_LEVEL", raw.get("log_level", "INFO"))).upper(),
        log_file_path=str(os.getenv("AI_AUDIT_LOG_FILE_PATH", raw.get("log_file_path", ""))),
        rag_collection=str(os.getenv("AI_AUDIT_RAG_COLLECTION", raw.get("rag_collection", "audit_docs"))),
        rag_ingest_url=str(os.getenv("AI_AUDIT_RAG_INGEST_URL", raw.get("rag_ingest_url", "http://192.168.1.128:9050/ingest"))),
    )


def load_architecture_rules(path: str | Path = "config/architecture_rules.yaml") -> dict[str, Any]:
    raw = _read_yaml(path)
    rules = raw.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("architecture_rules.yaml must contain rules as a list")
    return {"rules": rules}


def load_prompts(path: str | Path = "config/prompts.yaml") -> dict[str, str]:
    raw = _read_yaml(path)
    prompts = raw.get("prompts", {})
    if not isinstance(prompts, dict):
        raise ValueError("prompts.yaml must contain prompts map")
    return {str(k): str(v) for k, v in prompts.items()}


def load_model_provider_config(path: str | Path = "config/generator.yaml") -> ModelProviderConfig:
    raw = _read_yaml(path)
    provider = str(os.getenv("AI_AUDIT_LLM_PROVIDER", raw.get("provider", "ollama"))).lower()
    providers = raw.get("providers", {})
    selected = providers.get(provider, {})
    if not selected:
        selected = {
            "model": "deepseek-coder-33b",
            "endpoint": "http://localhost:11434/api/generate",
            "timeout_s": 90,
            "api_key_env": "",
            "temperature": 0.1,
        }
    return ModelProviderConfig(
        provider=provider,
        model=str(os.getenv("AI_AUDIT_LLM_MODEL", selected.get("model", "deepseek-coder-33b"))),
        endpoint=str(os.getenv("AI_AUDIT_LLM_ENDPOINT", selected.get("endpoint", "http://localhost:11434/api/generate"))),
        timeout_s=int(os.getenv("AI_AUDIT_LLM_TIMEOUT_S", str(selected.get("timeout_s", 90)))),
        api_key_env=str(os.getenv("AI_AUDIT_LLM_API_KEY_ENV", selected.get("api_key_env", ""))),
        temperature=float(os.getenv("AI_AUDIT_LLM_TEMPERATURE", str(selected.get("temperature", 0.1)))),
    )


def load_embedder_provider_config(path: str | Path = "config/embedder.yaml") -> EmbedderProviderConfig:
    raw = _read_yaml(path)
    provider = str(os.getenv("AI_AUDIT_EMBEDDER_PROVIDER", raw.get("provider", "local-hash"))).lower()
    providers = raw.get("providers", {})
    selected = providers.get(provider, {})
    if not selected:
        selected = {
            "model": "local-hash",
            "dim": 64,
            "endpoint": "",
            "api_key_env": "",
        }
    return EmbedderProviderConfig(
        provider=provider,
        model=str(os.getenv("AI_AUDIT_EMBEDDER_MODEL", selected.get("model", "nomic-embed-code"))),
        dim=int(os.getenv("AI_AUDIT_EMBEDDER_DIM", str(selected.get("dim", 64)))),
        endpoint=str(os.getenv("AI_AUDIT_EMBEDDER_ENDPOINT", selected.get("endpoint", ""))),
        api_key_env=str(os.getenv("AI_AUDIT_EMBEDDER_API_KEY_ENV", selected.get("api_key_env", ""))),
    )


def get_secret_from_env(env_name: str) -> str:
    if not env_name:
        return ""
    return os.getenv(env_name, "")
