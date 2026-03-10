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

### 1) Run a repo audit

```bash
ai-audit repo-run
```

Representative output:

```text
Repo audit completed.
- findings: 15
- high severity: 11
- reports written:
  - reports/nightly_repo_report.md
  - reports/security_report.md
```

### 2) Run an ecosystem audit

```bash
ai-audit ecosystem-run
```

Representative output:

```text
Ecosystem audit completed.
- repositories scanned: 1
- code units indexed: 164
- architecture violations: 0
- pattern proposals: 10
- report: reports/ecosystem_report.md
```

### 3) Ask a cross-repo question

```bash
ai-audit ask "What breaks if I modify /users/login?" --deep --iterations 3
```

Representative output:

```text
Likely impact areas:
1) auth middleware contracts
2) session/token validation paths
3) integration tests touching login flows
Suggested next step: run targeted repo audit after the change.
```

### 4) Run benchmark and extraction flow

```bash
ai-audit qa-benchmark --json
ai-audit approve-extraction --candidate-id ext-auth-001
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