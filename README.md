# AI Code Auditor

`ai-code-auditor` is a local-first platform for nightly code auditing, ecosystem governance, and cross-repo copilot queries.

## Quick Start

1. Create environment file:
   - `cp .env.example .env`
   - Update values in `.env` for your LAN services.
2. Install dependencies:
   - `python -m venv .venv && source .venv/bin/activate`
   - `pip install -e .[dev]`
3. Run DB migrations (Alembic):
   - `alembic upgrade head`
4. Configure repositories in `config/ecosystem.yaml`.
   - Optional provider configs:
     - `config/generator.yaml`
     - `config/embedder.yaml`
5. Run nightly jobs:
   - `python pipelines/nightly_repo_audit.py`
   - `python pipelines/ecosystem_audit.py`
6. Ask the copilot:
   - `ai-audit ask "Which services depend on billing?"`
   - Deep research mode:
     - `ai-audit ask "What breaks if I modify /users/login?" --deep --iterations 3`
7. Run correctness benchmark:
   - `ai-audit qa-benchmark --json`
8. Approve a shared-package extraction candidate:
   - `ai-audit approve-extraction --candidate-id ext-auth-001`

## Docker

- Build and run with:
  - `cp .env.example .env`
  - `docker compose up --build`

## Generated Artifacts

- `reports/nightly_repo_report.md`
- `reports/security_report.md`
- `reports/ecosystem_report.md`
- `reports/architecture_violations.md`
- `reports/pattern_proposals.md`
- `reports/extraction_candidates.yaml`
- `reports/extraction_candidates.md`
- `graph/ecosystem_graph.db`
- `index/code_embeddings.faiss`
