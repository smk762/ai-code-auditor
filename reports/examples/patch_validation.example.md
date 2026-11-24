# Patch Validation

> Real output from a scan of three invented repos, with no model. Rebuild: `scripts/build_example_reports.py`.

Responses from `POST /audit/validate_patch`, one patch per pack.

| Patch | Repo | Pack | Checks passed | Pack status | Evidence | Reason |
|---|---|---|---|---|---|---|
| Fix with a test that fails before it | orders-api | subprocess_popen_with_shell_equals_true | yes | resolved | repro | 1 test(s) fail on dev and pass with the fix: tests/test_health.py::test_host_is_one_argument |
| Fix without a test | billing | hashlib | yes | unverified | none | no test added or changed: add one that fails on dev and passes with the fix, or pass evidence_waiver |
| Fix that breaks an existing test | orders-api | hardcoded_sql_expressions | no | open | none | validation did not pass: FAILED tests/test_pricing.py::test_shipping - AssertionError: assert 14 == 15; E AssertionError: assert 14 == 15; E + where 14 = total([{'price': 10}]) |
