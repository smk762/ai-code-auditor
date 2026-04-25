# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]          # adds ai-audit CLI entry point
alembic upgrade head           # create/migrate audit DB (Postgres or SQLite)

# Optional semantic search
pip install -e .[semantic]     # adds faiss-cpu
```

## Commands

```bash
# CLI (all commands run from repo root with venv active)
ai-audit repo-run              # nightly single-repo audit
ai-audit ecosystem-run         # full multi-repo ecosystem audit
ai-audit ask "question"        # copilot Q&A over indexed code
ai-audit ask "..." --deep --iterations 4 --enable-subagents --agent-brief
ai-audit qa-benchmark --json
ai-audit approve-extraction --candidate-id ext-retry-core-001

# Direct pipeline scripts (same effect as CLI commands)
python pipelines/ecosystem_audit.py
python pipelines/nightly_repo_audit.py

# Tests
python -m pytest tests/ -v
python -m pytest tests/test_aggregator.py -v   # single test file

# Docker (API always-on + one-off audit)
docker compose up -d --build audit-api
docker compose run --rm --no-deps auditor      # one-off ecosystem audit
docker compose logs -f audit-api
```

## Configuration files

| File | Purpose |
|------|---------|
| `config/ecosystem.yaml` | Repo list, output paths, RAG ingest URL |
| `config/generator.yaml` | LLM provider/model selection (default: `ollama` → `qwen2.5-coder:32b`) |
| `config/embedder.yaml` | Embedder provider/model (default: `local-hash`) |
| `config/architecture_rules.yaml` | Cross-repo boundary and call-graph rules |
| `config/prompts.yaml` | LLM prompt templates |
| `config/sshfs_mounts.yaml` | SSHFS pairs for remote repos (see mount notes below) |

Override any config value with env vars — all keys in `auditor/config.py` have `AI_AUDIT_*` prefixes. Copy `.env.example` → `.env`.

## Repo mounts

Remote repos are mounted via `~/mount_ecosystem.sh` into `/home/smk/tmp/mnt/`. All `path:` entries in `config/ecosystem.yaml` use that prefix. `agent-composer` is local at `/home/smk/agent-composer`. `docker-compose.yml` bind-mounts `/home/smk/tmp/mnt` and `/home/smk/agent-composer` directly.

Repos not yet on this host (`tss-stack`, `sauron`) are set `enabled: false` in `ecosystem.yaml` — enable once migrated. Playact repos (`playact-engine`, `playact-frontend`) are scanned but excluded from service-role and call-graph rules.

## Architecture

### Audit pipeline (`pipelines/ecosystem_audit.py` → `ai-audit ecosystem-run`)

1. **Repo scan** (`auditor/repo_scanner.py`) — walks configured repos, respects `subpath`, returns source files.
2. **Code unit extraction** (`auditor/chunker.py`) — AST-aware extraction of functions/classes → `CodeUnit` dataclass.
3. **Static analysis** (`auditor/static_tools.py`) — runs bandit + semgrep if available; returns `Finding` list.
4. **LLM analysis** (`auditor/llm_client.py`) — sends each `CodeUnit` to the configured model; builds prompts from `auditor/ecosystem_briefing.py` context.
5. **Ecosystem graph** (`ecosystem/graph_builder.py`) — SQLite graph of repos/units/relationships, used for cross-repo queries.
6. **Pattern mining** (`patterns/`) — clusters, scores, and proposes extraction candidates.
7. **Reports** (`auditor/reporter.py`) — writes markdown + YAML to `reports/`.
8. **Embeddings** (`semantic/embedding_index.py`) — FAISS index of code units for copilot Q&A.

### Key data contracts (`auditor/contracts.py`)

- `CodeUnit` — id, repo, file_path, symbol, kind, language, start/end lines, raw_text
- `Finding` — id, type, severity (CRITICAL/HIGH/MEDIUM/LOW/INFO), repo, file, line, title, evidence, recommendation, source
- `RuleViolation` — architecture rule hit
- `CopilotResponse` — answer, citations, impacted_repos, confidence

### HTTP API (`auditor/audit_api.py`, port 8765)

Single-slot run model: only one ecosystem audit runs at a time. `POST /audit/run` starts a subprocess running `pipelines/ecosystem_audit.py` and returns `{run_id}` immediately. Poll `GET /audit/runs/{run_id}` or stream `GET /audit/runs/{run_id}/stream` (SSE). `POST /audit/ingest` pushes the latest reports to the RAG collection in rag-ingest.

### Copilot (`copilot/copilot_engine.py`)

Query routing (`copilot/query_router.py`) classifies queries → appropriate retrieval path. `ContextOrchestrator` budgets semantic hits + report snippets + notes under a token cap. `--deep` mode runs iterative research with up to N refinement loops. `--agent-brief` appends a structured brief for handing off to an external agent.

### LLM client (`auditor/llm_client.py`)

Supports `ollama` (POST `/api/generate`) and `openai-compatible` (POST `/v1/chat/completions`). Provider selected by `AI_AUDIT_LLM_PROVIDER` env var or `config/generator.yaml`. The healthcheck hits `/api/tags` for Ollama (fast probe, no model load) rather than generate.

## RAG ingest integration

`config/ecosystem.yaml` → `rag_ingest_url` points to the rag-ingest service in agent-composer. On this host that is `http://127.0.0.1:9050/ingest` (or the LAN IP if running from Docker). The `audit_docs` Qdrant collection is separate from the chat `project_docs` collection. Ingest is triggered via `POST /audit/ingest` on the audit API or `scripts/ingest_ecosystem_to_rag.py` directly.

## Generated artifacts

All written to `reports/` (overwritten each run): `nightly_repo_report.md`, `security_report.md`, `ecosystem_report.md`, `architecture_violations.md`, `pattern_proposals.md`, `extraction_candidates.yaml/md`. Graph: `graph/ecosystem_graph.db`. Embeddings: `index/code_embeddings.faiss`.
