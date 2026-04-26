"""Generate a compact per-repo ecosystem briefing for injection into audit prompts.

The briefing gives the model situational awareness without fine-tuning:
  - What this repo does and who owns it in the stack
  - Its upstream callers and downstream dependencies (from the graph DB)
  - Its exposed API endpoints (from api_mapper)
  - Cross-repo patterns and security boundaries

Briefing size is kept within a configurable token budget so smaller models aren't
overwhelmed.  The full ecosystem description is only included once at pipeline
start; per-file prompts get a compact repo-scoped summary.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from ecosystem.api_mapper import discover_api_endpoints

if TYPE_CHECKING:
    from ecosystem.graph_builder import GraphBuilder


# ---------------------------------------------------------------------------
# Ecosystem role table — parsed from ecosystem.md on first access.
# Keeps the briefing current as ecosystem.md evolves without a separate config.
# ---------------------------------------------------------------------------

_ECOSYSTEM_MD = Path(__file__).resolve().parents[1] / "ecosystem" / "ecosystem.md"

# Matches table rows like: | Core API | kimini | FastAPI ... |
_TABLE_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*\[?(\w[\w-]*)\]?[^|]*\|\s*([^|]+?)\s*\|", re.MULTILINE)


def _load_repo_roles() -> dict[str, dict[str, str]]:
    """Return {repo_name: {label, role}} parsed from ecosystem.md."""
    roles: dict[str, dict[str, str]] = {}
    if not _ECOSYSTEM_MD.exists():
        return roles
    text = _ECOSYSTEM_MD.read_text(encoding="utf-8")
    for m in _TABLE_ROW.finditer(text):
        label, repo, role = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if repo and role and label.lower() not in {"label", "---"}:
            roles[repo] = {"label": label, "role": role}
    return roles


_REPO_ROLES: dict[str, dict[str, str]] | None = None


def _get_repo_roles() -> dict[str, dict[str, str]]:
    global _REPO_ROLES
    if _REPO_ROLES is None:
        _REPO_ROLES = _load_repo_roles()
    return _REPO_ROLES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_ecosystem_overview(max_chars: int = 2000) -> str:
    """Return a compact summary of the full ecosystem (used once per pipeline run)."""
    roles = _get_repo_roles()
    if not roles:
        return ""
    lines = [
        "## Ecosystem Overview",
        "This codebase is a multi-service homelab stack. Repos and their roles:",
        "",
    ]
    for repo, info in roles.items():
        lines.append(f"- **{repo}** ({info['label']}): {info['role']}")
    lines += [
        "",
        "Architecture: clients → kimini-api (core) → TaskIQ workers → GPU services (imogen/vidita/tss-stack).",
        "Shared data layer: PostgreSQL, Redis, MinIO, Qdrant.",
        "Orchestration: gothmog (LangGraph). Observability: sauron (Prometheus/Grafana/Loki).",
        "Auth boundary: all external traffic through kimini-api. GPU services are LAN-only.",
    ]
    result = "\n".join(lines)
    return result[:max_chars]


def build_repo_briefing(
    repo_name: str,
    repo_path: Path,
    graph: "GraphBuilder | None" = None,
    max_chars: int = 1200,
) -> str:
    """Return a per-repo context block for injection into audit prompts.

    Includes: role in the stack, upstream callers, downstream dependencies,
    discovered API endpoints, and relevant security notes.
    """
    roles = _get_repo_roles()
    lines: list[str] = ["## Repo Context"]

    # Role
    info = roles.get(repo_name)
    if info:
        lines.append(f"**{repo_name}** — {info['label']}: {info['role']}")
    else:
        lines.append(f"**{repo_name}** — role not recorded in ecosystem.md")

    # Graph relationships
    if graph is not None:
        deps = graph.query_dependencies(repo_name)
        callers = graph.query_callers(repo_name)
        if deps:
            lines.append(f"Depends on: {', '.join(deps)}")
        if callers:
            lines.append(f"Called by: {', '.join(callers)}")

    # API endpoints
    endpoints: list[str] = []
    for path in repo_path.rglob("*.py"):
        if any(p in path.parts for p in {".git", "node_modules", ".venv", "__pycache__"}):
            continue
        endpoints.extend(discover_api_endpoints(path))
    if endpoints:
        sample = endpoints[:12]
        lines.append(f"Exposes endpoints: {', '.join(sample)}")
        if len(endpoints) > 12:
            lines.append(f"  … and {len(endpoints) - 12} more")

    # Security notes derived from role
    _add_security_notes(repo_name, info, lines)

    result = "\n".join(lines)
    return result[:max_chars]


def build_file_briefing(
    repo_name: str,
    file_path: Path,
    repo_path: Path,
    graph: "GraphBuilder | None" = None,
) -> str:
    """One-line context prepended to per-file audit prompts.

    Keeps per-file prompts small while still giving role context.
    """
    roles = _get_repo_roles()
    info = roles.get(repo_name, {})
    role_hint = info.get("label", repo_name)
    rel = file_path.relative_to(repo_path) if file_path.is_relative_to(repo_path) else file_path.name
    return f"[{role_hint} / {repo_name}] Auditing: {rel}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SECURITY_NOTES: dict[str, list[str]] = {
    "kimini-api": [
        "Auth boundary: all external requests enter here. JWT validation, rate limiting, and role checks are critical.",
        "Generation proxies must not leak backend service URLs or credentials to clients.",
    ],
    "imogen": [
        "LAN-only GPU service. Should never accept unauthenticated external requests.",
        "The /unload endpoint frees GPU VRAM — misuse could cause service disruption.",
        "File paths passed to model inference must be validated to prevent path traversal.",
    ],
    "vidita": [
        "LAN-only GPU service. Redis queue keys (vidq:*) must not be accessible externally.",
        "MinIO presigned URLs must have short TTLs; check for URL exposure in logs or responses.",
    ],
    "gothmog": [
        "Orchestrator with LLM tool calls. Prompt injection via user-controlled inputs could redirect tool calls.",
        "Executes multi-step workflows — check for TOCTOU races in state transitions.",
    ],
    "agent-composer": [
        "RAG chat + ingest service. User queries reach the LLM — sanitise inputs that feed into retrieval or generation.",
        "Signed ingest endpoint — HMAC validation must be applied before any Qdrant writes; replay window (nonce DB) must be checked on every request.",
        "Cloudflare Zero Trust is the auth layer; do not add routes that bypass it.",
    ],
}


def _add_security_notes(
    repo_name: str,
    info: dict[str, str] | None,
    lines: list[str],
) -> None:
    notes = _SECURITY_NOTES.get(repo_name, [])
    if notes:
        lines.append("Security context:")
        for note in notes:
            lines.append(f"  • {note}")
