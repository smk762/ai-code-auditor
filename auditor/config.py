from __future__ import annotations

from dataclasses import dataclass
import logging
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
    rag_ingest_url: str = "http://127.0.0.1:9050/ingest"


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


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _yaml_bool(value: Any, key: str, default: bool) -> bool:
    """A YAML flag. ``bool("false")`` is True, so a quoted ``"false"`` must be parsed, not coerced;
    anything that isn't a recognisable boolean is a config error rather than a guess."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise ValueError(f"config: {key} must be a boolean, got {value!r}")


def load_ecosystem_config(path: str | Path = "config/ecosystem.yaml") -> EcosystemConfig:
    raw = _read_yaml(path)
    repos = [
        RepoConfig(
            name=item["name"],
            path=item["path"],
            branch=item.get("branch", ""),
            enabled=_yaml_bool(item.get("enabled"), f"repos[{item.get('name')}].enabled", True),
            access_token_env=item.get("access_token_env", ""),
            provider=item.get("provider", "local"),
            local_cache_path=item.get("local_cache_path", ""),
            subpath=str(item.get("subpath", "") or ""),
        )
        for item in raw.get("repos") or []  # a bare `repos:` is None
    ]
    enabled = [r for r in repos if r.enabled]
    if not enabled:
        raise ValueError("config/ecosystem.yaml must define at least one enabled repository in repos[].")
    return EcosystemConfig(
        repos=enabled,
        output_dir=os.getenv("AI_AUDIT_OUTPUT_DIR", raw.get("output_dir", "reports")),
        graph_db_path=os.getenv("AI_AUDIT_GRAPH_DB_PATH", raw.get("graph_db_path", "graph/ecosystem_graph.db")),
        embedding_index_path=os.getenv("AI_AUDIT_EMBEDDING_INDEX_PATH", raw.get("embedding_index_path", "index/code_embeddings.faiss")),
        architecture_memory_path=os.getenv(
            "AI_AUDIT_ARCH_MEMORY_PATH",
            raw.get("architecture_memory_path", "memory/architecture_memory.md"),
        ),
        # Off unless configured, by design: the API's mutating endpoints are gated by
        # AI_AUDIT_API_TOKEN (``_require_mutating_auth``) and the CLI/timers run locally on the host.
        auth_mode=_bool_env("AI_AUDIT_AUTH_MODE", _yaml_bool(raw.get("auth_mode"), "auth_mode", False)),
        auth_code_env=os.getenv("AI_AUDIT_AUTH_CODE_ENV", raw.get("auth_code_env", "AI_AUDIT_AUTH_CODE")),
        log_level=str(os.getenv("AI_AUDIT_LOG_LEVEL", raw.get("log_level", "INFO"))).upper(),
        log_file_path=str(os.getenv("AI_AUDIT_LOG_FILE_PATH", raw.get("log_file_path", ""))),
        rag_collection=str(os.getenv("AI_AUDIT_RAG_COLLECTION", raw.get("rag_collection", "audit_docs"))),
        rag_ingest_url=str(os.getenv("AI_AUDIT_RAG_INGEST_URL", raw.get("rag_ingest_url", "http://127.0.0.1:9050/ingest"))),
    )


def approved_rules_path() -> str:
    """Where approved rules (``use_<library>`` from extraction approvals) go: a file outside git,
    merged into the rules on load. Docker mounts ``rules/`` so the API can write it."""
    return os.getenv("AI_AUDIT_ARCH_RULES_PATH", "").strip() or "rules/architecture_rules.json"


def approved_rules(path: str | Path | None = None) -> list[dict[str, Any]]:
    """The approved rules (JSON or YAML; a missing file is none)."""
    raw = _read_yaml(path or approved_rules_path())
    rules = raw.get("rules") if isinstance(raw, dict) else None
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ValueError(f"{path or approved_rules_path()}: 'rules' must be a list")
    kept = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        if "forbid" not in r and any(k in r for k in ("required_imports", "forbidden_patterns")):
            # what the old approve-extraction wrote: never evaluated, so it checked nothing
            logging.getLogger(__name__).warning(
                "%s: ignoring %r, an old extraction approval that checks nothing; re-approve the candidate "
                "on /ui/architecture", path or approved_rules_path(), r.get("name"))
            continue
        kept.append(r)
    return kept


def load_architecture_rules(path: str | Path = "config/architecture_rules.yaml", *,
                            approved: str | Path | None = None,
                            repos: set[str] | None = None) -> dict[str, Any]:
    """The rules file plus the approved rules (``approved_rules_path``) it doesn't already name.

    Validated on load (``ecosystem.arch_contracts.validate``): a role or repo that doesn't resolve,
    or a rule that could match nothing, raises ValueError. *repos* is the set of known repo names,
    by default every repo in ``config/ecosystem.yaml`` and ``config/repo_mirrors.yaml``."""
    raw = _read_yaml(path)
    rules = raw.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("architecture_rules.yaml must contain rules as a list")
    extra_path = Path(approved or approved_rules_path())
    if extra_path.resolve() != Path(path).resolve():
        names = {r.get("name") for r in rules if isinstance(r, dict)}
        rules = [*rules, *(r for r in approved_rules(extra_path) if r.get("name") not in names)]
    domain_ownership = raw.get("domain_ownership", [])
    if not isinstance(domain_ownership, list):
        raise ValueError("architecture_rules.yaml domain_ownership must be a list")
    service_roles = raw.get("service_roles", [])
    call_graph_rules = raw.get("call_graph_rules", {})
    data = {
        "rules": rules,
        "domain_ownership": domain_ownership,
        "service_roles": service_roles,
        "call_graph_rules": call_graph_rules,
    }
    from ecosystem import arch_contracts

    arch_contracts.validate(data, repos if repos is not None else arch_contracts.known_repos())
    return data


def load_prompts(path: str | Path = "config/prompts.yaml") -> dict[str, str]:
    raw = _read_yaml(path)
    prompts = raw.get("prompts", {})
    if not isinstance(prompts, dict):
        raise ValueError("prompts.yaml must contain prompts map")
    return {str(k): str(v) for k, v in prompts.items()}


def _select_provider(raw: dict[str, Any], provider: str, default_provider: str, kind: str) -> dict[str, Any]:
    """Return the ``providers`` block for ``provider``.

    Only the built-in default provider may run without a block (it gets hardcoded defaults); any
    other name with no block is a typo or a missing config, so it raises rather than pairing the
    requested provider name with another provider's model and endpoint.
    """
    providers = raw.get("providers") or {}
    if not isinstance(providers, dict):
        raise ValueError(f"{kind} config: providers must be a mapping, got {type(providers).__name__}")
    selected = providers.get(provider) or {}
    if not isinstance(selected, dict):
        raise ValueError(f"{kind} config: providers.{provider} must be a mapping")
    if not selected and provider != default_provider:
        known = ", ".join(sorted(str(k) for k in providers)) or "none"
        raise ValueError(
            f"{kind} provider {provider!r} has no block under providers (known: {known}); "
            f"fix the provider name or add its config"
        )
    return selected


def load_model_provider_config(path: str | Path = "config/generator.yaml") -> ModelProviderConfig:
    raw = _read_yaml(path)
    provider = str(os.getenv("AI_AUDIT_LLM_PROVIDER", raw.get("provider", "ollama"))).lower()
    selected = _select_provider(raw, provider, "ollama", "LLM")
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
    selected = _select_provider(raw, provider, "local-hash", "embedder")
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
