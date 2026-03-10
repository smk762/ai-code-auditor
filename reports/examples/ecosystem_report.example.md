# Ecosystem Report (Example)

- Ecosystem Health Score: 92%
- Repositories scanned: 4
- Code units indexed: 2,418
- Architecture violations: 3
- Pattern proposals: 12
- Extraction candidates: 2

## Repository Coverage
- `api-gateway` (services, auth middleware, request contracts)
- `billing-service` (invoice workflows, retry logic, provider adapters)
- `identity-service` (token issuance, session validation, role checks)
- `shared-utils` (http clients, observability helpers, validation primitives)

## Architecture Violations

### 1) Forbidden dependency edge
- Rule: `services must not import web handlers`
- Source: `billing-service/services/invoice_service.py`
- Target: `billing-service/web/handlers/invoice_handler.py`
- Severity: MEDIUM
- Recommended fix: Move shared logic into `billing-service/domain/`.

### 2) Cross-boundary auth coupling
- Rule: `api-gateway cannot read identity persistence layer directly`
- Source: `api-gateway/auth/session_guard.py`
- Target: `identity-service/storage/user_repo.py`
- Severity: HIGH
- Recommended fix: Access identity data through `identity-service` API contract.

### 3) Circular utility import path
- Rule: `shared-utils must remain leaf dependency`
- Source: `shared-utils/http/client.py`
- Target: `api-gateway/integrations/http_wrapper.py`
- Severity: LOW
- Recommended fix: Consolidate wrappers into one shared module.

## Pattern Proposals (Top 3)

### Proposal: Standardized retry wrapper
- Confidence: 0.87
- Affected repos: `api-gateway`, `billing-service`
- Benefit: Remove duplicate retry/backoff boilerplate and unify error handling.
- Suggested action: Introduce `shared-utils/retry.py` with one policy surface.

### Proposal: Unified auth context object
- Confidence: 0.81
- Affected repos: `api-gateway`, `identity-service`
- Benefit: Reduce drift in claim parsing and role enforcement.
- Suggested action: Add typed auth context helper and migrate middlewares.

### Proposal: Consistent error payload schema
- Confidence: 0.78
- Affected repos: all scanned repositories
- Benefit: Better client compatibility and simpler observability dashboards.
- Suggested action: Adopt one `error_code/message/context` contract.

## Candidate Feedback Snapshot
- `ext-auth-ctx-002`: Analyst marked as **promising**, requested migration risk notes.
- `ext-retry-core-001`: Platform engineer marked as **high-value**, approved for pilot.
- Team sentiment: prioritize low-risk extractions that reduce repeated operational bugs.

## Interpretation
- Ecosystem posture is healthy but has high-value architectural cleanup opportunities.
- Feedback indicates teams prefer pragmatic extraction candidates with clear rollout plans.
- Next run should validate whether approved proposals reduce duplicate findings.
