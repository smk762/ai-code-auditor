# AI Code Auditor - Current Product Spec

This document replaces legacy `spec1.md`, `spec2.md`, and `spec3.md` and reflects the implemented codebase state.

## 1) Mission

`ai-code-auditor` is a local-first, nightly auditing platform for multi-repository ecosystems.  
It combines static analysis, LLM-assisted review, architecture governance, pattern discovery, shared-package extraction proposals, and copilot-style ecosystem Q&A.

## 2) Implemented Scope (As-Built)

### 2.1 Pipelines

- `pipelines/nightly_repo_audit.py`
  - Scans configured repos.
  - Extracts code units and runs LLM + static checks.
  - Dedupe/normalizes findings.
  - Persists run state and findings.
  - Generates reports and archives markdown artifacts.
- `pipelines/ecosystem_audit.py`
  - Re-scans code units across repos.
  - Rebuilds graph + embedding index.
  - Runs pattern mining and architecture rule evaluation.
  - Generates extraction candidates (Section 21 flow).
  - Writes ecosystem reports and archives artifacts.

### 2.2 Core Modules

- `auditor/`
  - contracts (`CodeUnit`, `Finding`, `RuleViolation`, `PatternProposal`, `CopilotResponse`)
  - config/env loading (`.env`, yaml + env overrides)
  - auth (JWT verification + RBAC checks)
  - runtime/logging metadata
  - persistence (SQLAlchemy models + DB writes)
  - retry/locking/archive utilities
- `ecosystem/`
  - graph builder (SQLite graph store)
  - architecture engine
  - basic API/schema discovery helpers
- `patterns/`
  - mining/clustering/scoring/proposals
  - extraction candidate builder/scorer/migration planner
  - pattern feedback and architecture rule append
- `semantic/`
  - embedding index + semantic search
- `copilot/`
  - query router and copilot answer synthesis
  - extraction-candidate-aware query handling

### 2.3 CLI Surface

- `ai-audit repo-run`
- `ai-audit ecosystem-run`
- `ai-audit ask "<query>" [--deep] [--iterations N] [--json]`
- `ai-audit qa-benchmark [--dataset <path>] [--json]`
- `ai-audit approve-extraction --candidate-id <id>`

### 2.4 Outputs

- Repo reports:
  - `reports/nightly_repo_report.md`
  - `reports/security_report.md`
- Ecosystem reports:
  - `reports/ecosystem_report.md`
  - `reports/architecture_violations.md`
  - `reports/pattern_proposals.md`
  - `reports/extraction_candidates.yaml`
  - `reports/extraction_candidates.md`
- Persistence/data:
  - PostgreSQL tables via Alembic migrations
  - `graph/ecosystem_graph.db`
  - `index/code_embeddings.faiss` (+ meta)

## 3) Runtime and Infrastructure

- Env-driven runtime via `.env` (template: `.env.example`).
- LAN-compatible service settings for:
  - PostgreSQL
  - Redis
  - MinIO (S3 API)
  - Ollama / model endpoints
- Docker entrypoint runs `alembic upgrade head` before pipeline.
- Security controls:
  - auth code gate (optional)
  - JWT role checks (`admin`, `analyst`)
  - auth audit events persisted
  - token-safe remote git access (no token in URL/args)

### 3.1 Duplicate Detection Tuning Knobs

Hybrid duplicate detection and extraction gating are configurable via `.env`:

- `AI_AUDIT_DUP_AST_MIN` (default `0.72`)
- `AI_AUDIT_DUP_EMBED_MIN` (default `0.78`)
- `AI_AUDIT_DUP_SIG_MIN` (default `0.60`)
- `AI_AUDIT_DUP_LSH_BANDS` (default `8`)
- `AI_AUDIT_DUP_LSH_ROWS` (default `8`)

## 4) Section 21 (Shared Package Discovery) - Implemented

Implemented cross-repo extraction candidate flow:

- Inputs:
  - duplicate clusters
  - git churn metrics
  - persisted findings (quality pressure)
  - graph context placeholder object
- Scoring:
  - weighted extraction formula with normalized sub-scores and priority bands
- Outputs:
  - machine-readable YAML report
  - human-readable markdown report
- Approval flow:
  - `approve-extraction` appends generated architecture rule to `config/architecture_rules.yaml`
- Copilot:
  - responds to extraction-related queries using generated candidate reports

## 5) Data Model (Persisted)

Alembic/SQLAlchemy tables:

- `audit_runs`
- `repo_runs`
- `findings`
- `pipeline_checkpoints`
- `auth_audit_events`

## 6) Remaining TODOs Before MVP

These are the highest-impact gaps between current implementation and production-ready MVP quality.

1. **Duplicate detector calibration**
   - Tune LSH/AST/embedding/signature thresholds on larger multi-repo fixtures and add non-Python structural normalization.
2. **Dependency impact depth**
   - Implement true downstream impact scoring from graph traversal (currently heuristic).
3. **Flaky-test density signal**
   - Add flaky test ingestion/metrics as an explicit extraction candidate signal.
4. **Architecture rule lifecycle**
   - Add explicit approved/rejected candidate state store; avoid relying only on report files.
5. **Graph backend consistency**
   - Align graph persistence strategy (SQLite graph db + relational metadata) with clear source-of-truth semantics.
6. **Provider contract hardening**
   - Add strict schema validation for provider responses and richer fallback/error classification.
7. **Operational preflight**
   - Add `preflight` command to verify DB/Redis/MinIO/LLM health before runs.
8. **Incremental processing**
   - Add changed-file-only mode with cache invalidation rules for faster nightly runs.
9. **Benchmark coverage**
   - Expand `qa/fixtures` to multi-repo realistic datasets and enforce precision/recall thresholds in CI.
10. **Observability integration**
   - Emit structured metrics suitable for centralized monitoring (run latency, failure rates, candidate drift).

## 7) MVP Readiness Gate

MVP is considered ready when:

- nightly pipelines complete reliably on target repos for multiple consecutive runs,
- extraction candidates are non-trivial and validated by humans,
- copilot answers are backed by report/graph citations,
- auth and audit logging are enforced in deployed mode,
- benchmark thresholds are met on realistic fixtures.
