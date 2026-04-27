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
4. Configure repositories in `config/ecosystem.yaml` (see [Ecosystem configuration](#ecosystem-configuration) below).
   - Optional provider configs:
     - `config/generator.yaml`
     - `config/embedder.yaml`

## Ecosystem configuration

Repositories are listed under `repos` in `config/ecosystem.yaml`. Each entry needs a `name` and a `path`. Optional `subpath` limits scanning to a folder inside that repository (empty means the whole tree).

**Local directory** — scan a clone on disk (relative or absolute). The current Git checkout is what gets scanned; `branch` in YAML is not applied.

```yaml
repos:
  - name: my-service
    path: ../my-service
    provider: local
    subpath: ""
```

**Mounted path** (homelab share, bind-mount, NFS, etc.) — same as local; use the mountpoint path.

```yaml
repos:
  - name: my-service
    path: /mnt/ai-audit/my-service
    provider: local
    subpath: ""
```

**Remote (HTTPS)** — the tool clones or updates under `local_cache_path` (default `.cache/repos`). Use `branch` for the branch to check out. For private repositories, set `access_token_env` to an environment variable that holds a token (see `.env.example`).

```yaml
repos:
  - name: my-service
    path: https://github.com/org/my-service.git
    branch: main
    provider: remote
    access_token_env: GITHUB_TOKEN
    local_cache_path: .cache/repos
    subpath: ""
```

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
ai-audit ask "If we rotate JWT signing keys and move to short-lived access tokens this sprint, which services, middleware contracts, and test suites will break first, and what is the safest migration order?" --deep --iterations 4 --context-profile deep --enable-subagents --agent-brief
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
External Agent Brief:
- objective, constraints, prioritized tasks, and verification checklist included
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

### 8) Prepare a brief for an external/paid agent

```bash
ai-audit ask "We need an external agent to execute JWT key rotation safely across gateway, identity, and billing. Build a high-signal execution brief with migration order, risks, and verification gates." \
  --context-profile deep \
  --enable-subagents \
  --agent-brief
```

Representative output:

```text
Potential blast radius touches repos: api-gateway, identity-service, billing-service.
Context summary:
- Semantic hits: 5
- Report snippets: 3
- Note snippets: 1
- Context curated to prioritize direct evidence and avoid low-signal verbosity.

External Agent Brief
Objective: execute JWT key rotation safely across core services
Primary repos in scope: api-gateway, identity-service, billing-service
Constraints:
- minimal high-signal context only
- cite evidence for every critical claim
Deliverables expected:
- top risks with severity and blast radius
- phased migration order and rollback points
- PR checklist and verification plan
```

## Ecosystem SSHFS (single switch)

If repos live on a remote host under `/mnt/ai-audit/...`, define all SSHFS pairs in [`config/sshfs_mounts.yaml`](config/sshfs_mounts.yaml) and use **one** systemd user unit to mount everything: [`contrib/systemd/user/README.md`](contrib/systemd/user/README.md).

## Docker

**Yes — the `auditor` service is a batch job** (ecosystem audit once, then exit). In production you usually:

- Keep **`audit-api`** running (daemon).
- Run **`auditor` on a schedule** (cron or systemd timer), not only when you happen to `docker compose up`.

### Typical commands

```bash
cp .env.example .env

# API only (good default for “always on”)
docker compose up -d --build audit-api

# One-off ecosystem audit (same image/command as the auditor service)
docker compose run --rm --no-deps auditor

# Dev / smoke: start API + run one audit immediately on boot
docker compose up -d --build
docker compose logs -f --tail 200
```

`audit-api` listens on port **8765** (see `.env` / `AUDIT_API_PORT`). Logs: `docker compose logs -f audit-api`.

### Ecosystem repo paths in Docker

`config/ecosystem.yaml` uses **absolute host paths**. `docker-compose.yml` already bind-mounts the two prefixes used there: **`/mnt/ai-audit`** (SSHFS / shared audit trees) and **`/home/smk/GITHUB/smk762`** (imogen, vidita, loraline). On another machine, edit those volume lines so the left side matches your host.

If every repo still logs **`path not found, skipping`**, the host paths are missing (e.g. SSHFS not mounted) or you need an extra volume for a new `path:` prefix.

**Redis / DB from the container:** `REDIS_URL` / `DATABASE_URL` must point at an address the **container** can open (often your LAN host IP, e.g. `192.168.1.121`, not `127.0.0.1` on the host unless you use host networking).

### Daily schedule (example)

Use a **systemd timer** or **cron** that runs `docker compose run --rm --no-deps auditor` from the repo directory. Copy/adjust: [`contrib/systemd/user/ai-audit-docker-ecosystem.service`](contrib/systemd/user/ai-audit-docker-ecosystem.service) and [`.timer`](contrib/systemd/user/ai-audit-docker-ecosystem.timer).

Repo-only nightly scan (no Docker): `python pipelines/nightly_repo_audit.py` — use when the stack runs on the host instead of a container.

[Compose profiles](https://docs.docker.com/compose/how-tos/profiles/) are reserved for optional stacks (e.g. a future **`tests`** service); core services are not hidden behind profiles by default.

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