# AI Code Auditor

`ai-code-auditor` helps engineering teams catch risk early, surface architecture drift, and discover high-leverage refactors across multiple repositories.

## What It Does (and Why That's Awesome)

Most audit tools tell you what is wrong in one repo.  
This project gives you an ecosystem-wide intelligence layer you can run locally:

- Nightly repo audits for security, quality, and maintainability signals
- Cross-repo ecosystem analysis with architecture and pattern detection
- Copilot-style Q&A over your code graph and indexed code units
- Shared-package extraction candidates to reduce duplication and churn

Why teams star this kind of tool:

- **Faster decisions:** Ask impact questions before merging risky changes.
- **Safer releases:** Catch high-risk findings nightly, not after incidents.
- **Cleaner architecture:** Track violations and pattern drift over time.
- **Real outputs, not vapor:** Reports and artifacts are written to disk every run.

If this saves your team time, give it a star and share it with your platform/devex crew.

## Clone and Run

```bash
git clone https://github.com/smk762/ai-code-auditor.git
cd ai-code-auditor
```

## Quick Start

1. Create environment file:
   - `cp .env.example .env`
   - Update values in `.env` for your LAN services.
2. Install dependencies:
   - `python -m venv .venv && source .venv/bin/activate`
   - `pip install -e .[dev]`
3. Run DB migrations:
   - `alembic upgrade head`
4. Configure repositories in `config/ecosystem.yaml`.
   - Optional provider configs:
     - `config/generator.yaml`
     - `config/embedder.yaml`

## CLI Commands + Representative Responses

### 1) Show available commands

```bash
ai-audit --help
```

Representative output:

```text
Usage: ai-audit [OPTIONS] COMMAND [ARGS]...

Commands:
  repo-run
  ecosystem-run
  ask
  qa-benchmark
  approve-extraction
```

### 2) Run a repo audit

```bash
ai-audit repo-run
```

Representative output:

```text
Repo audit completed.
- findings: 24
- critical: 2
- high severity: 9
- reports written:
  - reports/nightly_repo_report.md
  - reports/security_report.md
```

### 3) Run an ecosystem audit

```bash
ai-audit ecosystem-run
```

Representative output:

```text
Ecosystem audit completed.
- repositories scanned: 4
- code units indexed: 2418
- architecture violations: 3
- pattern proposals: 12
- extraction candidates: 2
- report: reports/ecosystem_report.md
```

### 4) Ask a cross-repo question (deep mode)

```bash
ai-audit ask "If we rotate JWT signing keys and move to short-lived access tokens this sprint, which services, middleware contracts, and test suites will break first, and what is the safest migration order?" --deep --iterations 4
```

Representative output:

```text
High-risk impact map:
1) api-gateway auth middleware (token verification assumptions)
2) identity-service session refresh flow (expiry/rotation contract mismatch)
3) billing-service service-to-service token cache (stale key risk)
4) integration and e2e suites asserting legacy token lifetime behavior
Recommended migration order:
1) dual-sign/dual-verify key rollout
2) middleware contract update
3) downstream service token refresh alignment
4) test suite policy updates + audit rerun
```

### 5) Run QA benchmark

```bash
ai-audit qa-benchmark --json
```

Representative output:

```text
{
  "dataset": "qa/fixtures/benchmark.json",
  "questions_total": 50,
  "correct": 43,
  "accuracy": 0.86,
  "status": "pass"
}
```

### 6) Approve an extraction candidate

```bash
ai-audit approve-extraction --candidate-id ext-retry-core-001
```

Representative output:

```text
Extraction candidate approved: ext-retry-core-001
- architecture rule appended: config/architecture_rules.yaml
- decision audit entry recorded
```

### 7) Run legacy pipeline scripts directly (optional)

```bash
python pipelines/nightly_repo_audit.py
python pipelines/ecosystem_audit.py
```

Representative output:

```text
Pipeline completed.
- repo artifacts refreshed in reports/
- graph rebuilt: graph/ecosystem_graph.db
- embedding index refreshed: index/code_embeddings.faiss
```

## Docker

```bash
cp .env.example .env
docker compose up --build
```

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

## Example Reports

Preview realistic report formats before running your own:

- [Nightly Repo Report Example](reports/examples/nightly_repo_report.example.md)
- [Security Report Example](reports/examples/security_report.example.md)
- [Ecosystem Report Example](reports/examples/ecosystem_report.example.md)
- [Extraction Candidates Example](reports/examples/extraction_candidates.example.md)