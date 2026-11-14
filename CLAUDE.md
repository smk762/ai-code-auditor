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
ai-audit packs --json          # discover agent coding packs (no paste)
ai-audit packs --bundle --repo argus-cortex   # one-call mission bundle (world model + specialists)
ai-audit packs --set-status claimed --id pack-…  # lifecycle: open|claimed|in_progress|resolved|wontfix
ai-audit packs --id pack-… --brief
ai-audit merge-poll --schedule               # hourly GitHub API → pack/RAG on dev tip moves (+ PR intel refresh)
ai-audit pr-intel                            # blocked PRs, why, suggested resolution path (read-only; --json, --cached)
ai-audit merge-refresh --repo argus-cortex --schedule  # one-repo pack/RAG (or --now)
ai-audit repair --pack-id pack-… --mode review   # agent-composer repair loop driven by a pack (fetches pack, no diff needed)
ai-audit repair --pack-id pack-… --audit-api-base "$AI_AUDIT_PUBLIC_BASE_URL"   # real URL in local .env (see .env.example)
ai-audit qa-benchmark --json
ai-audit approve-extraction --candidate-id ext-retry-core-001
bash scripts/install_overnight_timers.sh         # rest 00:30 + ecosystem 02:00 user timers

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
| `config/repo_mirrors.yaml` | Local git mirrors for Argus + agent-composer + voluptas (`dev`) |
| `config/rest_windows.yaml` | Night conductor rest window + wrap-up budget |

Override any config value with env vars — all keys in `auditor/config.py` have `AI_AUDIT_*` prefixes. Copy `.env.example` → `.env`.

## Repo mirrors

Scan set is all `smk762/argus-*` plus `agent-composer` and `voluptas` (Argus consumes both). Paths live under `/home/smk/<name>` or `/home/smk/mirrors/<name>`. `ai-audit rest-run` auto-syncs mirrors and checks out **`dev`** before scanning. Manual sync: `bash scripts/sync_repo_mirrors.sh`. SSHFS mounts are deprecated.

Canonical topology: [`ecosystem/ecosystem.md`](ecosystem/ecosystem.md).

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

Web UI (`auditor/ui.py`): static pages in `auditor/static/` served at `/` (landing, data from `/audit/summary.json`) and `/audit/usage`, with assets mounted at `/audit/static/`. Every page links the shared `ui.css` (tokens, light/dark) and `ui.js` (fetch and format helpers, theme toggle), and puts `<!--#nav-->` where the server injects the shared nav. Each page has its own JSON endpoint and there is no build step. Markdown goes through `render_markdown()` on the server (raw HTML escaped, no inline styles) so the internal UI and the public wiki export share one renderer. Read-only pages need no auth; any action goes through `_require_mutating_auth`.

Runs and findings pages (`auditor/ui_runs.py`): `/ui/runs`, `/ui/runs/{id}` (live through the SSE stream) and `/ui/findings`, backed by `/audit/runs.json`, `/audit/runs/{id}/summary.json` and `/audit/findings.json`; `/ui/reports/{name}` renders a report from `reports/`. Only repo audits store findings rows; ecosystem runs store none. A run is compared with the latest earlier finished run of the same pipeline that scanned an overlapping repo set. Findings match on repo, file, type and title (not line), and then LLM rewordings in the same file are paired by title word overlap. Finished-ok statuses are `success`/`partial_success` from the pipeline and `completed` from `POST /audit/run` (`auditor/run_views.py`).

### Copilot (`copilot/copilot_engine.py`)

Query routing (`copilot/query_router.py`) classifies queries → appropriate retrieval path. `ContextOrchestrator` budgets semantic hits + report snippets + notes under a token cap. `--deep` mode runs iterative research with up to N refinement loops. `--agent-brief` appends a structured brief for handing off to an external agent.

### LLM client (`auditor/llm_client.py`)

Supports `ollama` (POST `/api/generate`) and `openai-compatible` (POST `/v1/chat/completions`). Provider selected by `AI_AUDIT_LLM_PROVIDER` env var or `config/generator.yaml`. The healthcheck hits `/api/tags` for Ollama (fast probe, no model load) rather than generate.

## RAG ingest integration

`config/ecosystem.yaml` → `rag_ingest_url` points to the rag-ingest service in agent-composer. On this host that is `http://127.0.0.1:9050/ingest` (or the LAN IP if running from Docker). The `audit_docs` Qdrant collection is separate from the chat `project_docs` collection. Ingest is triggered via `POST /audit/ingest` on the audit API or `scripts/ingest_ecosystem_to_rag.py` directly.

## Generated artifacts

All written to `reports/` (overwritten each run): `pr_intel.json` (blocked-PR intel, hourly; UI at `GET /audit/prs`, data `GET /audit/prs.json`, also spoken in the morning briefing), `nightly_repo_report.md`, `security_report.md`, `ecosystem_report.md`, `architecture_violations.md`, `pattern_proposals.md`, `extraction_candidates.yaml/md`. Agent coding packs: `reports/agent_packs/` (+ `INDEX.json`; discover via `GET /audit/packs`). Graph: `graph/ecosystem_graph.db`. Embeddings: `index/code_embeddings.faiss`.

## Branches

`dev` is the default branch and the base for every PR. Cut feature branches from `origin/dev` and bring them up to date by merging `origin/dev`, never `main`. Never push work to `main` or open a PR against it, except the release PR from `dev`: `main` must never be ahead of `dev`, with one exception. A release fast-forwards `main` to a `dev` commit that passed CI (`git push origin origin/dev:main`), then tags it. If `main` is protected, the release is a reviewed PR from `dev` into `main`, and merging it leaves a merge commit on `main` alone. That merge commit is the one exception to the rule: it leaves `main` one commit ahead of `dev` with an identical tree. Leave it there and don't merge `main` back into `dev`. Anything else on `main` that `dev` lacks is a mistake (`git diff origin/dev origin/main` must be empty). Once `main` carries such a commit it can't be fast-forwarded, so later releases are PRs too.

## Agent coding packs (Cursor / Claude)

Loop: **scan → compressed world model → specialist coding packs**. Agents look packs up themselves (`AGENTS.md`, `.cursor/rules/agent-coding-packs.mdc`):

```bash
ai-audit packs --kind world_model --json          # ecosystem + per-repo memory
ai-audit packs --layer specialist --repo <name> --json
ai-audit packs --bundle --repo <name>                   # == GET /audit/packs/bundle?repo=<name>&token_budget=2500
ai-audit packs --id <pack-id> --brief
```

Lifecycle: `POST /audit/packs/{id}/status {status, note?}` (mutating auth); `validate_patch` with `pack_id` auto-resolves on pass; conductor draft PRs set `in_progress`. Pack RAG ingest is on by default (`AI_AUDIT_PACK_RAG_INGEST=0` to disable).

Consuming-agent flow: `bundle?repo=` → `POST /audit/packs/{id}/status {"status":"claimed"}` → `ai-audit repair --pack-id <id>` (or implement from the brief) → `POST /audit/validate_patch` with `pack_id` (pass ⇒ `resolved`). Full detail in `AGENTS.md`.

Cross-repo awareness: `bash scripts/install_agent_pack_awareness.sh [--dry-run] [--repo NAME]` writes an idempotent `AGENTS.md` section + `.cursor/rules/agent-coding-packs.mdc` into every enabled ecosystem repo on disk (local files only; it never runs git in those repos). Re-run after changing the API base or flow text.

Refresh full loop: `python scripts/emit_agent_packs.py`. Ecosystem / nightly / conductor / diff-audit also emit packs. Merges to `dev` → hourly `ai-audit merge-poll` (GitHub API) or `POST /audit/merge` (see `contrib/github/merge-refresh-webhook.md`).
