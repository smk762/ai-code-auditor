# Security Report

> Real output from a scan of three invented repos, with no model. Rebuild: `scripts/build_example_reports.py`.

- Security findings: 7

- [LOW] orders-api `orders-api/orders_api/health.py`:1 - blacklist
- [HIGH] orders-api `orders-api/orders_api/health.py`:5 - subprocess_popen_with_shell_equals_true
- [MEDIUM] orders-api `orders-api/orders_api/store.py`:2 - hardcoded_sql_expressions
- [LOW] orders-api `orders-api/tests/test_pricing.py`:5 - assert_used
- [HIGH] billing `billing/billing/receipts.py`:5 - hashlib
- [LOW] billing `billing/tests/test_receipts.py`:5 - assert_used
- [MEDIUM] storefront `storefront/storefront/invoices.py`:9 - request_without_timeout
