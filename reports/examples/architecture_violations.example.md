# Architecture Violations

> Real output from a scan of three invented repos, with no model. Rebuild: `scripts/build_example_reports.py`.

- Total violations: 2
  - Graph rule violations: 1
  - Call graph violations (direct inter-service calls): 1
  - Domain boundary violations (misplaced logic): 0
  - UI in worker violations: 0
  - Competing provider paths: 0

## Call Graph Violations

These services make direct HTTP calls to a target they should not reach. All inter-service calls should flow through the canonical gateway/orchestrator.

### storefront → billing
- Severity: HIGH
- Entities: storefront, billing
- Evidence: Unauthorised direct call from 'storefront' to 'billing'. Locations: invoices.py:9 (requests.get())

## Graph Rule Violations

### service_boundary
- Severity: MEDIUM
- Path: storefront -> orders-api
- Entities: storefront, orders-api
- Evidence: storefront (ui) imports orders-api (worker): storefront/summary.py:1 (orders_api.pricing)
