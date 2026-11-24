# Extraction Candidates

> Real output from a scan of three invented repos, with no model. Rebuild: `scripts/build_example_reports.py`.

## Extract normalize_email (2 repos)
- Score: 0.4042
- Priority: low
- Repos: billing, orders-api
- Why extract:
  - Similar normalize_email logic appears across 2 repos.
  - Cluster churn in 90d: 2 changes.
  - Quality pressure signals across cluster files: 0.
- Proposed package API:
  - normalize_email(input: dict) -> dict
- Phased migration plan:
  - phase_1: Create compatibility wrapper
    - Create shared-logic package with stable API.
    - Add adapters preserving old function signatures.
  - phase_2: Adopt in low-risk services first
    - Migrate imports in billing to shared-logic.
  - phase_3: Ecosystem rollout and cleanup
    - Migrate orders-api to shared-logic.
    - Remove duplicate helpers.
    - Enforce architecture rule requiring shared-logic.
- Risks and rollback notes:
  - No major extraction blockers detected; validate integration tests before rollout.
