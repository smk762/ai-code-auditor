# Nightly Repo Report

> Real output from a scan of three invented repos, with no model. Rebuild: `scripts/build_example_reports.py`.

## Summary
- Total findings: 8
- Critical: 0
- High: 2
- Medium: 2
- Low: 4

## Findings

### blacklist
- Severity: LOW
- Type: security
- Repo: orders-api
- File: `orders-api/orders_api/health.py`:1
- Source: bandit
- Description: Consider possible security implications associated with the subprocess module.
- Recommendation: Review and remediate this security finding.

### subprocess_popen_with_shell_equals_true
- Severity: HIGH
- Type: security
- Repo: orders-api
- File: `orders-api/orders_api/health.py`:5
- Source: bandit
- Description: subprocess call with shell=True identified, security issue.
- Recommendation: Review and remediate this security finding.

### hardcoded_sql_expressions
- Severity: MEDIUM
- Type: security
- Repo: orders-api
- File: `orders-api/orders_api/store.py`:2
- Source: bandit
- Description: Possible SQL injection vector through string-based query construction.
- Recommendation: Review and remediate this security finding.

### assert_used
- Severity: LOW
- Type: security
- Repo: orders-api
- File: `orders-api/tests/test_pricing.py`:5
- Source: bandit
- Description: Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- Recommendation: Review and remediate this security finding.

### hashlib
- Severity: HIGH
- Type: security
- Repo: billing
- File: `billing/billing/receipts.py`:5
- Source: bandit
- Description: Use of weak MD5 hash for security. Consider usedforsecurity=False
- Recommendation: Review and remediate this security finding.

### assert_used
- Severity: LOW
- Type: security
- Repo: billing
- File: `billing/tests/test_receipts.py`:5
- Source: bandit
- Description: Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- Recommendation: Review and remediate this security finding.

### Workaround comment: HACK: the gateway drops the first call after idle
- Severity: LOW
- Type: maintainability
- Repo: billing
- File: `billing/billing/retry.py`:5
- Source: workaround
- Description: A comment says the code around it works around a problem. Agents copy what is in the repo, so the workaround spreads unless the cause is fixed.
- Recommendation: Fix the cause the comment names, then delete the workaround and the comment. If it must stay, say why it is permanent and mark the line `workaround: ok`.

### request_without_timeout
- Severity: MEDIUM
- Type: security
- Repo: storefront
- File: `storefront/storefront/invoices.py`:9
- Source: bandit
- Description: Call to requests without timeout
- Recommendation: Review and remediate this security finding.
