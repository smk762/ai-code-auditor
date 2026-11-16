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
ai-audit search "question" --repo argus-cortex --kind wiki   # semantic search over audit_docs (via rag-chat)
ai-audit ask "..." --deep --iterations 4 --enable-subagents --agent-brief
ai-audit packs --json          # discover agent coding packs (no paste)
ai-audit packs --bundle --repo argus-cortex   # one-call mission bundle (world model + specialists)
ai-audit packs --set-status claimed --id pack-…  # lifecycle: open|claimed|in_progress|unverified|resolved|wontfix
ai-audit packs --id pack-… --brief
ai-audit packs --dedupe --dry-run              # list duplicate packs (same file, reworded title); drop --dry-run to close them as wontfix
ai-audit merge-poll --schedule               # hourly GitHub API → pack/RAG on dev tip moves (+ PR intel refresh)
ai-audit pr-intel                            # blocked PRs, why, suggested resolution path (read-only; --json, --cached)
ai-audit merge-refresh --repo argus-cortex --schedule  # one-repo pack/RAG (or --now)
ai-audit repair --pack-id pack-… --mode review   # agent-composer repair loop driven by a pack (fetches pack, no diff needed)
ai-audit repair --pack-id pack-… --audit-api-base "$AI_AUDIT_PUBLIC_BASE_URL"   # real URL in local .env (see .env.example); rag-chat must allow it (its AUDIT_API_URL / AI_AUDIT_PUBLIC_BASE_URL, or AUDIT_API_ALLOWED_HOSTS), else 422
ai-audit pack-savings                        # estimated token savings from packs vs crawling (--history, --cached, --json)
ai-audit trust [--github]                    # trust metrics: lint-only resolutions, regressions, reverts (--history, --cached, --json)
ai-audit qa-benchmark --json
ai-audit wiki build --repos argus-cortex --dry-run   # price a wiki build (no API calls)
ai-audit wiki build [--repos a,b] [--sync]          # generate wiki pages via the Batch API
ai-audit wiki status                                # built repos + wiki lane budget
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

Scan set is all `smk762/argus-*` plus `agent-composer` and `voluptas` (Argus consumes both). Paths live under `/home/smk/<name>` or `/home/smk/mirrors/<name>`. `ai-audit rest-run` auto-syncs mirrors and checks out **`dev`** before scanning. Manual sync: `bash scripts/sync_repo_mirrors.sh`. A mirror is fast-forwarded; when uncommitted changes block that (the old tip is an ancestor of `origin/dev`), they are stashed first and the outcome is `fetch+ff-stashed`. If `dev` was rewritten upstream and every local commit is already there (`git cherry`, or a merge into upstream that would change nothing), the mirror is reset to `origin/dev`: the old tip goes to `backup/pre-sync-<sha>` and uncommitted changes are stashed. A mirror with any unique local commit is left as-is and reported as `fetch+diverged`. SSHFS mounts are deprecated.

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

Web UI (`auditor/ui.py`): static pages in `auditor/static/` served at `/` (landing, data from `/audit/summary.json`) and `/audit/usage`, with assets mounted at `/audit/static/`. Every page links the shared `ui.css` (tokens, light/dark) and `ui.js` (fetch and format helpers, theme toggle), and puts `<!--#nav-->` where the server injects the shared nav. Each page has its own JSON endpoint and there is no build step. Markdown goes through `render_markdown()` on the server (raw HTML escaped, no inline styles) so the internal UI and the public wiki export share one renderer. Read-only pages need no auth; any action goes through `_require_mutating_auth`. On this host that is `AI_AUDIT_API_TOKEN` (`X-API-Token`); the same value is in agent-composer's `.env` and in the `env` block of `~/.claude/settings.json`, so coding agents in any repo have it.

Runs and findings pages (`auditor/ui_runs.py`): `/ui/runs`, `/ui/runs/{id}` (live through the SSE stream) and `/ui/findings`, backed by `/audit/runs.json`, `/audit/runs/{id}/summary.json` and `/audit/findings.json`; `/ui/reports/{name}` renders a report from `reports/`. Only repo audits store findings rows; ecosystem runs store none. A run is compared with the latest earlier finished run of the same pipeline that scanned an overlapping repo set. Findings match on repo, file, type and title (not line), and then LLM rewordings in the same file are paired by title word overlap. `/ui/findings` also shows a trend (stacked columns by severity over the last 30 runs that stored findings, from `history` in the JSON: after each run every repo counts with its latest audit, so a single-repo run only moves its own repo) and +/− against the baseline run in each matrix cell (`matrix_prev`, raw counts), with a sparkline per repo. Those counts come from scans and change only on the next scan; `pack_matrix` (pack status per repo, and resolved packs by severity) shows agent work before then: the headline line, "N resolved" under each repo total, and hatching on the viewed run's column. Finished-ok statuses are `success`/`partial_success` from the pipeline and `completed` from `POST /audit/run` (`auditor/run_views.py`). Each finding row carries its agent coding pack (`pack: {id, status, kind}`, matched on repo, file path and title from `agent_packs/INDEX.json`; when several match, active work wins: in progress, claimed, open, then wontfix and resolved), `pack_status=` filters on it (`none` = no pack), and "Copy agent prompt" hands the filtered open packs to a coding agent as pack ids plus the claim → fix → `validate_patch` loop, not as pasted finding text.

Repos and packs pages (`auditor/ui_repos.py`): `/ui/repos` (`/audit/repos/summary.json`) shows one card per repo: the `dev` tip from `merge_poll_state.json` with its commit date read from the local mirror, a "mirror behind" flag when the mirror's HEAD differs from that tip, and the repo's last audit, findings, packs by status and architecture violations (from `architecture_violations.json`, falling back to the markdown). `/ui/packs` (`/audit/packs/board.json`) shows packs in lifecycle columns, and `/ui/packs/{id}` is a server-rendered brief with a status form. Actions go through `UI.send()` in `ui.js`: on a 401 it asks for the API token or auth code, keeps it in `sessionStorage` and sends it as both `X-API-Token` and `X-Auth-Code`.

Trust metrics (`auditor/trust_metrics.py`, epic #64, issue #52): `/ui/trust` (`/audit/trust.json[?history=1]`, `POST /audit/trust/refresh[?github=1]` with auth) measures whether agent fixes can be trusted. Every later trust-upgrade change is judged against it.
- **Metrics:**
  - **False-resolve:** validator resolutions with `tool=lint_only`.
  - **Reopen:** resolved, then open/claimed/in_progress again.
  - **Recurrence:** a resolved finding pack's issue comes back, from a later pack or a later scan, matched one-to-one with `finding_match`. Split into `disputed` (closed without a patch as "already handled" while the scanner still reports it) and `regressed`. Only regressions count in the rate. Precision is limited by title matching.
  - **Scan persistence:** the share of a scan's findings the next scan of the repo re-reports. That's the detector's stability, so read recurrence against it.
  - **Agent PR revert / fix-forward rate:** reverts and fix-forwards within 14 days.
  - **Weekly finding flow** by type.
- **Data sources:** pack YAMLs, the findings DB (only pipelines that store findings count as scans, so ecosystem runs don't look like empty scans) and the mirrors' `git log`. GitHub only lists agent PRs: a pack id in the body, or a title that fixes ai-code-auditor findings. That list is cached in `reports/trust_prs.json`.
- **Emit-time link:** a new pack that repeats a resolved pack gets `governance.recurrence_of` (`link_recurrences`).
- **When it runs:** after every nightly repo audit, once the run is recorded (`AI_AUDIT_TRUST_METRICS=0` turns it off). It writes `reports/trust.json` and appends `trust_history.jsonl`, which is never overwritten.

Ecosystem map (`auditor/ui_topology.py`): `/ui/topology` (`/audit/topology/map.json`); `/audit/topology` redirects there, and the old generated HTML is at `/audit/topology/legacy`. Edges come from four sources:
- **Documented flow:** the diagram in `ecosystem/ecosystem.md`, horizontal `──►` and vertical `│ ▼` arrows.
- **Imports:** `import argus_<x>` in each mirror's non-test source.
- **Service use:** the `SERVICES` regex catalogue, which needs at least 2 files to draw an edge; single mentions are listed in the node's details. ComfyUI points at voluptas and rag-chat/ingest at agent-composer.
- **Violations:** policy violations from `service_topology.json`.

The scan is cached until a mirror HEAD, the doc or the sidecar changes. In the page (d3 v7 from cdnjs), each node is pulled to a computed target: pipeline columns by flow depth, cortex beneath them, the other repos in rows, services in side columns and a top row. The force layout only resolves overlaps.

Repo nodes use the Argus package marks from argus-cortex `brand/` (`auditor/brand.py`, served at `/audit/brand/packages/{pkg}.svg?variant=light|dark|mono`). The marks are read from the cortex mirror, or fetched once from GitHub `dev` into `reports/_brand/`; SVGs with scripts or event handlers are refused. Repos without a mark get a ring with one eye. The eye colour is the brand stage (data / training / apps / foundation). It is an accent only, because those colours fail as a categorical palette, so every node is labelled and the panel names the stage.

Type tags (`_facets` in `auditor/ui_topology.py`) are read from each mirror's tracked files: UI (a frontend or UI deps in `package.json`), API (FastAPI/Flask/Starlette/express routes), CLI (`[project.scripts]`, typer/click/argparse), library (a pyproject project), infra (`.tf`), CI (reusable `workflow_call` workflows) and template (copier/cookiecutter). The map shows at most two tags per repo; the panel lists every tag with evidence alongside the declared role. Service logos live in `auditor/static/service-icons.json`, built by `scripts/build_service_icons.py`: Simple Icons paths (CC0, pinned version), plus generic glyphs where Simple Icons has none (OpenAI, NudeNet, SingGuard, diffusers, kohya, mithrandir).

Night and architecture pages: `/ui/night` (`auditor/ui_night.py`, `/audit/night.json?run=`) reads each rest session from `reports/conductor/<run_id>/*.json` and the matching mithrandir record. Mission outcomes come from the mission files, because the mithrandir worker stats don't count `PARTIAL`. Spend comes from the ledger's `conductor` lane, because `cost_usd_est` is not filled in. `/ui/architecture` (`auditor/ui_arch.py`) shows violations, pattern proposals and extraction candidates. Approve (`POST /audit/extraction/{id}/approve`, auth) appends the `use_<lib>` rule to `AI_AUDIT_ARCH_RULES_PATH` (default `config/architecture_rules.yaml`). `append_architecture_rule_for_extraction` now inserts the rule as text so the file's comments survive.

### Copilot (`copilot/copilot_engine.py`)

Query routing (`copilot/query_router.py`) classifies queries → appropriate retrieval path. `ContextOrchestrator` budgets semantic hits + report snippets + notes under a token cap. `--deep` mode runs iterative research with up to N refinement loops. `--agent-brief` appends a structured brief for handing off to an external agent.

### LLM client (`auditor/llm_client.py`)

The review prompt is `AUDIT_INSTRUCTIONS_V2` (default, chosen by the 2026-11-13 eval); `AI_AUDIT_LLM_PROMPT=v3` adds state/ordering lenses and is opt-in until measured. `repo_review` in `config/prompts.yaml` is not what the scan uses.

Supports `ollama` (POST `/api/generate`) and `openai-compatible` (POST `/v1/chat/completions`). Provider selected by `AI_AUDIT_LLM_PROVIDER` env var or `config/generator.yaml`. The healthcheck hits `/api/tags` for Ollama (fast probe, no model load) rather than generate.

## Generated wiki (`wiki/`)

The pipeline is notes → drafts → check-and-fix → citation pass, all through the Message Batches API (`wiki/batch.py`):
- **Notes:** per-file Haiku notes (`wiki/notes.py`), cached by path+content hash under `reports/wiki/_notes/`. The 2026-11-13 eval's cache in `reports/wiki_eval/notes/` is read as a fallback.
- **Drafts and check-and-fix:** Opus at medium effort drafts four pages per repo (`wiki/pages.py`) from the notes, README, `ecosystem/ecosystem.md` and AST structure facts. A second Opus pass checks each page against the exact cited code.
- **Citation pass** (`wiki/citations.py`): drops any claim whose `[path:Lx-Ly]` citation is missing, out of range, or names no identifier that occurs in the cited lines.
- **Findings page:** "Notable findings" is built from data (`wiki/findings.py`), not by a model.

Output is `reports/wiki/<repo>/*.md` plus `INDEX.json` (source SHA, page tree, per-page citation stats, dropped claims, and content hashes of the cited files), and `reports/wiki/INDEX.json` for all repos. `_raw/` keeps the text from before the citation pass.

Viewer (`auditor/ui_wiki.py`): `/ui/wiki` lists the built repos, and `/ui/wiki/{repo}/{page}` renders a page server-side with a sidebar of the repo's pages. Citations become links to the GitHub blob at the SHA the page was built from (the repo's `subpath` is prefixed). Mermaid blocks are shown as source. The behind badge compares the built SHA with the `dev` tip in `merge_poll_state.json`: it shows a commit count when the local mirror has the tip, otherwise just "behind dev". JSON: `/audit/wiki`, `/audit/wiki/{repo}`, `/audit/wiki/{repo}/{page}`. Page files are served only from the repo's own `INDEX.json` entries.

Batch `custom_id`s hash the exact request params, and results are stored as they are harvested, so reruns and crashes never pay twice. Each stage is priced first and checked against the `wiki` lane (default $30/month, `AI_AUDIT_BUDGET_WIKI_USD`). Only Python/JS/TS sources are read, so repos made of YAML, Terraform or Jinja get no pages yet.

Public export (`wiki/public.py`, `ai-audit wiki export`) writes the gated bundle for dragonhound.dev to `reports/wiki_public/`: `manifest.json` (dev SHA and sha256 per page), plus `<repo>/<page>.md` and `.html` (shared renderer, citations linked to GitHub at the SHA). dragonhound.dev is the only public copy, and there are no GitHub wikis.
- **Scope:** read from GitHub at export time: `argus-*` and public. A failed lookup counts as private.
- **Pages:** overview, architecture, interfaces and integration only. Never "Notable findings" or the internal INDEX data. Mermaid is omitted for now.
- **Opsec gate:** refuses a page outright, never redacts it, and fails closed. It refuses private or link-local IPs, home or mount paths, emails, LAN hostnames, names of private non-argus repos (from GitHub), and terms from the private deny-list at `~/.config/ai-code-auditor/opsec-denylist.txt` or `AI_AUDIT_OPSEC_DENYLIST`. That file is never committed, and the export won't run without it.
- **Refusals:** a repo with a refused page is left out whole. The refusal report, which quotes the leak, goes to `reports/wiki_public_report.json`, outside the bundle.
- **Bundle:** built output only: HTML fragments plus `manifest.json`. Its `bundle_sha256` covers the content files, not the manifest's timestamp, so an unchanged export keeps the same hash.
- **Public variant:** `ai-audit wiki build --public` regenerates pages from scrubbed inputs into `reports/wiki/_public/`, and the export prefers them.

Publish (`wiki/publish.py`, `ai-audit wiki publish [--push]`) commits the bundle to the private `smk762/argus-docs` repo, using a machine-managed checkout at `~/.cache/ai-code-auditor/argus-docs`.
- **Checks first:** it verifies the bundle hash and re-runs the gate.
- **Commits** as `argus-docs-bot <noreply@dragonhound.dev>`, and only when the bundle hash changed.
- **Output:** it prints the `docs.lock.json` values (repo, commit, bundle_sha256).
- **Going live:** dragonhound-site deploys by direct upload (not git-connected Pages) and pins docs in `docs.lock.json`. A pin bump on its `dev` is the review gate. Never add deploy hooks or commit docs into dragonhound-site.

Refresh (`wiki/refresh.py`, `ai-audit wiki refresh [--dry-run] [--force]`; weekly timer `ai-audit-wiki-refresh`, Sundays 04:30) rebuilds only stale pages, then exports and publishes.
- **What counts as stale:** a page is stale when the text at a line range it cites changed. Lines shifting under a citation also count. Ranges come from `cited_ranges` in INDEX; for pages built before ranges were recorded, they're reconstructed with `git show <source_sha>:<path>` from the mirror, and failing that the whole-file `cited_files` hash is used. Stale pages are rebuilt only when the mirror moved past the built SHA, and at most every `AI_AUDIT_WIKI_MIN_INTERVAL_DAYS` (default 7).
- **Budget:** each plan item is priced first. If the total won't fit 95% of what's left in the `wiki` lane, pages that reach the public bundle go first (oldest builds first), and the rest are deferred and recorded in `_refresh.json` rather than refusing the whole run.
- **No waiting:** failed or missing pages are retried on the next run.
- **Public variant:** kept only where needed, meaning repos that already have one or that the last export refused. `--all-public` builds it for every public repo.
- **Concurrency:** one run at a time (lock file). The last run is recorded in `reports/wiki/_refresh.json`.
- **API:** `POST /audit/wiki/{repo}/regenerate[?force=1]` (auth; the button on wiki pages) and `GET /audit/wiki/refresh`.

## RAG ingest integration

`config/ecosystem.yaml` → `rag_ingest_url` points to the rag-ingest service in agent-composer. On this host that is `http://127.0.0.1:9050/ingest` (or the LAN IP if running from Docker). Ingest is triggered via `POST /audit/ingest` on the audit API or `scripts/ingest_ecosystem_to_rag.py` directly.

Every sender goes through `auditor/rag_client.py` (`post_documents`), which speaks rag-ingest's current protocol.
- **Body:** `{"docs", "source", "tags"}`.
- **Headers:** `X-Timestamp`, a single-use `X-Nonce`, and `X-Signature` (HMAC-SHA256 keyed by sha256(`INGEST_SHARED_SECRET`) over `ts.nonce.sha256(body)`). `INGEST_SHARED_SECRET` in `.env` must match agent-composer's.
- **Metadata:** rag-ingest keeps only id, title, url, path and source per doc, so repo and kind go in `tags` (`kind:wiki`, `repo:<name>`).
- **Collection:** each request names `audit_docs` (`AI_AUDIT_RAG_COLLECTION`). rag-ingest accepts its own `QDRANT_COLLECTION` (default `audit_docs` since agent-composer#25) or one in `INGEST_ALLOWED_COLLECTIONS`, and refuses others with 400. `project_docs` is deprecated.
- **Replacing and retiring:** re-sending a doc id replaces its chunks (agent-composer#26). Ids that are no longer sent are retired with `POST /delete` (`rag_client.delete_documents`), using the record of ids sent per scope in `reports/rag_ingested.json` (`wiki:<repo>`, `packs`; `AI_AUDIT_RAG_LEDGER` overrides the path). A failed delete stays recorded and is retried on the next run. Ids are recorded per batch rag-ingest accepts, so a send that fails partway still records what landed.
  - **Wiki:** sections no longer in a repo's pages are retired. A refresh that rebuilt only some pages considers only those pages.
  - **Packs:** resolved and wontfix packs are retired instead of re-sent, so retrieval shows open work. `scripts/emit_agent_packs.py` (a full send) also retires packs that no longer exist. Every status writer (the API endpoint, `validate_patch` auto-resolve, `ai-audit packs --set-status`) re-sends or retires that pack through `refresh_pack_rag`. Partial sends, such as a status change or new packs, never retire other packs.

Search goes through rag-chat's `POST /search` (`rag_client.search`), so the query is embedded by the model that wrote the vectors. `ai-audit search "q" [--repo R] [--kind wiki|agent_coding_pack] [--top-k N] [--json]` and `GET /audit/search?q=&repo=&kind=&top_k=` (read-only, no auth) both return hits with title, link, tags and text. rag-chat is at `RAG_CHAT_URL`, or the rag-ingest host on port 9150 when that's unset. `RAG_CHAT_API_KEY` is sent as a bearer token when set. rag-chat's `/chat` retrieves from the same collection.

Wiki ingest (`wiki/rag.py`, `ai-audit wiki ingest`) sends one doc per `##` section of the four content pages (internal variant). Each has id `wiki::<repo>::<page>::<section>` and a link to the section. The refresh ingests the pages it rebuilt; `AI_AUDIT_WIKI_RAG_INGEST=0` turns it off. "Ask this repo" (`wiki/ask.py`, `POST /audit/wiki/{repo}/ask`, auth, `ask` spend lane) answers from the repo's wiki pages, notes and structure facts with the configured LLM. The context is prompt cached, so follow-ups cost ~10%. Answers go through the citation pass and link at the mirror HEAD. The bug icon under an answer files it as an open `bug` pack (`POST /audit/wiki/{repo}/report`, auth, `wiki/report.py`; source `ask`, located at the first citation), so a bug spotted in chat goes through the normal claim → repair → validate_patch loop. Reporting the same exchange and note twice gives the same pack.

## Generated artifacts

All written to `reports/` (overwritten each run): `pr_intel.json` (blocked-PR intel, hourly; UI at `GET /audit/prs`, data `GET /audit/prs.json`, also spoken in the morning briefing), `nightly_repo_report.md`, `security_report.md`, `ecosystem_report.md`, `architecture_violations.md`, `pattern_proposals.md`, `extraction_candidates.yaml/md`. `pack_savings.json` + `pack_savings_history.jsonl` (estimated token savings from packs, chars/4, re-measured after each pack emit; `GET /audit/pack-savings.json[?history=1]`; the history file is appended, never overwritten). Agent coding packs: `reports/agent_packs/` (+ `INDEX.json`; discover via `GET /audit/packs`). Graph: `graph/ecosystem_graph.db`. Embeddings: `index/code_embeddings.faiss`.

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

Dedupe (`auditor/finding_match.py`, the same title match the findings delta uses): a finding that repeats an existing pack (same repo and file, reworded title, from a different run) reuses that pack's id at emit time, so a claimed or wontfix pack keeps its status instead of getting an open twin. Resolved packs aren't merge targets (an issue that comes back after a fix gets a fresh pack), and architecture/extraction/invariants packs match on exact id only, because their titles don't describe the issue. `ai-audit packs --dedupe [--dry-run]` closes existing open duplicates as `wontfix` with `governance.duplicate_of`, keeping the pack furthest along in each file, and packs closed that way are never used as merge targets.

`validate_patch` (`auditor/validator.py`, `validate_patch_tree`) applies the patch to a clean tree of the repo's `dev` tip (`git archive`, so uncommitted work in the live checkout never leaks in), not a copy of the checkout. Mirrors only sync nightly, so `auditor/validate_base.py` first fetches `dev` from the mirror's `origin` URL into a bare cache repo (`<output_dir>/_validate_git/`, `AI_AUDIT_VALIDATE_GIT_DIR`) that borrows the mirror's objects through `alternates`. The mirror is never written: it is read-only in the container. A fetch is bounded (`AI_AUDIT_VALIDATE_FETCH_TIMEOUT_S`, default 20) and skipped when one ran in the last `AI_AUDIT_VALIDATE_FETCH_TTL_S` (default 60) seconds. When it fails, the mirror's `origin/dev` (else `HEAD`) is used. The response names the base: `base_ref`, `base_sha`, `base_source` (`fetched` / `cached` / `mirror` / `copy`), `base_fresh`, `base_fetch_error`, and `base_stale` / `dev_tip` / `behind_by` against the tip merge-poll last saw. A gitignored version module that hatch-vcs or setuptools-scm generates at build time (`version-file`, `version_file`, `write_to`) gets a stub in the temp tree, so it doesn't push validation to lint-only. Each changed `.py` file maps to its nearest project (a dir with `pyproject.toml`, `setup.py`, `requirements.txt` or `tests/`), so a monorepo like agent-composer runs `rag-chat/`'s tests for a rag-chat change. Changed files get a fatal-only `ruff` lint (E9,F63,F7,F82) first. Each project's tests run in a venv built from its declared deps (requirements files with `-r` includes, pyproject deps plus `dev`/`test` extras, never the package itself; tests import the patched tree via `PYTHONPATH`). Venvs are cached by deps hash in `AI_AUDIT_VALIDATE_VENV_DIR` (default `<output_dir>/_validate_venvs`), so the first run for a project waits for pip and later runs don't. When tests fail, the same project's tests run on the unpatched tree, and only failures the patch introduced count: tests already failing on `dev` are named in a note. When no tests can run (none collected, the env didn't build, a module the project doesn't declare is missing, or the file belongs to no project), the lint result stands as `lint_only` with a note saying why, and a lint-only pass still resolves the pack. TypeScript/JavaScript changes (`auditor/validate_node.py`) map to the nearest `package.json` (`web/`, `frontend/` or the root). That project's locked deps are installed once per lockfile hash with `npm ci --ignore-scripts` into `AI_AUDIT_VALIDATE_NODE_DIR` (default `<output_dir>/_validate_node`) and symlinked into the temp tree. Then the scripts it declares run in order: `typecheck`, `lint`, every `*:check` (`wire:check`, `darkroom:check`, `docs:check`: offline drift checks) and `test`. A failing check runs again on the base tree, and only errors the patch introduced count (tsc by file+code+message, eslint by file+rule+message, vitest by test id, anything else by output line, never by line number). A changed TS/JS file that got no check at all (npm missing, `npm ci` failed, no lockfile or no check scripts, or a `.ts` file with no `package.json` above it) makes the result skipped, never passed, so the pack stays open. A plain `.js` file outside any npm project gets `node --check`. `tool` is `npm_checks` (or `node_check`), joined with `+` for a mixed patch (`pytest+npm_checks`), and `suggest_test_commands` gives TS files the project's npm checks rather than pytest. The image copies in node 22 (vitest 4 needs node 20+). Patches with neither Python nor TS/JS changes use the repo-level validator.

Lifecycle: `POST /audit/packs/{id}/status {status, note?}` (mutating auth). `validate_patch` with `pack_id` resolves on a pass *with evidence* (#58, `auditor/evidence.py`), else sets `unverified`, which is still open work (in the default bundle filter, ranked lower) and the brief says what's missing.
- **Bug, security and untyped finding packs** need a test that fails on `dev` and passes patched. The patch's test material (test files, plus anything under `tests/` or `__tests__/`) is copied onto a second clean dev tree and run there: pytest, or vitest for TS projects whose test script uses it. A test that can't even be collected on dev doesn't count.
- **Other code changes** need tests or npm checks to have run (lint alone isn't enough). Test-only patches need their tests to have run. Docs/config-only patches pass as they are.
- **Waiver:** `evidence_waiver` (≥ 20 chars) resolves anyway and is recorded.
- **Where it's stored:** the evidence goes on `verification.evidence` (kind, repro test ids, per-tree outcomes, base SHA) and shows on `/ui/packs/{id}`. Packs now store `narrative.finding_type`. The trust metrics count resolutions by evidence kind and the waiver rate.
- **Old rule:** `AI_AUDIT_EVIDENCE_BAR=0` restores it.

Also on lifecycle: opt-in `semantic_check` / `AI_AUDIT_VALIDATE_SEMANTIC=1` runs `auditor/equivalence.py` first (a "trust me" change with a counterexample keeps the pack open), and conductor draft PRs set `in_progress`. Pack RAG ingest is on by default (`AI_AUDIT_PACK_RAG_INGEST=0` to disable).

Consuming-agent flow: `bundle?repo=` → `POST /audit/packs/{id}/status {"status":"claimed"}` → `ai-audit repair --pack-id <id>` (or implement from the brief) → `POST /audit/validate_patch` with `pack_id` (pass with evidence ⇒ `resolved`, pass without ⇒ `unverified`). Full detail in `AGENTS.md`.

Cross-repo awareness: `bash scripts/install_agent_pack_awareness.sh [--dry-run] [--repo NAME]` writes an idempotent `AGENTS.md` section + `.cursor/rules/agent-coding-packs.mdc` into every enabled ecosystem repo on disk (local files only; it never runs git in those repos). Re-run after changing the API base or flow text.

Refresh full loop: `python scripts/emit_agent_packs.py`. It replaces the world-model, architecture and extraction packs it rebuilds and leaves finding packs (nightly, diff_audit, ask, conductor) and their status alone. Ecosystem / nightly / conductor / diff-audit also emit packs. Merges to `dev` → hourly `ai-audit merge-poll` (GitHub API) or `POST /audit/merge` (see `contrib/github/merge-refresh-webhook.md`).
