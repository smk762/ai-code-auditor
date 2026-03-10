# Extraction Candidates (Example)

## Candidate Summary
- Candidates scored: 5
- Above approval threshold: 2
- Pending human review: 1
- Rejected this run: 2

## Top Candidates

### ext-retry-core-001
- Proposed package: `@ecosystem/retry-core`
- Repos impacted: `api-gateway`, `billing-service`, `shared-utils`
- Duplicate clusters linked: 4
- Extraction score: 0.84 (threshold: 0.75)
- Expected outcome:
  - ~420 duplicate lines removed
  - 18% fewer retry-related incidents based on historical issue tags
  - Faster policy updates through one shared implementation

### ext-auth-ctx-002
- Proposed package: `@ecosystem/auth-context`
- Repos impacted: `api-gateway`, `identity-service`
- Duplicate clusters linked: 3
- Extraction score: 0.79 (threshold: 0.75)
- Expected outcome:
  - Unified JWT claims parsing and role checks
  - Lower auth drift risk between middleware implementations
  - Better test reuse across auth-heavy services

## Rejected Candidates
- `ext-date-utils-005` (score: 0.58): low impact and limited reuse surface.
- `ext-error-codes-004` (score: 0.61): naming conflicts unresolved across repos.

## Reviewer Feedback

### Platform engineer review
- Decision: **Approve** `ext-retry-core-001`
- Feedback: "High operational value and low migration risk."
- Required follow-up: publish migration checklist before rollout.

### Security engineer review
- Decision: **Conditional approve** `ext-auth-ctx-002`
- Feedback: "Good standardization; require explicit token-validation test matrix."
- Required follow-up: add negative token tests for all consuming services.

### Application team review
- Decision: **Reject for now** `ext-error-codes-004`
- Feedback: "Cross-team contract not aligned; revisit after API contract freeze."

## Suggested Next Actions
1. Run `ai-audit approve-extraction --candidate-id ext-retry-core-001`.
2. Generate migration plan and owner map for affected repositories.
3. Re-score candidates after one release cycle to measure impact.
