# Agent Work Pack: subprocess_popen_with_shell_equals_true

> Real output from a scan of three invented repos, with no model. Rebuild: `scripts/build_example_reports.py`.

- **id:** `pack-orders-api-80259b67a3`
- **kind:** nightly
- **severity:** HIGH
- **status:** open
- **repo:** orders-api
- **location:** `orders_api/health.py`:5

## Objective
subprocess call with shell=True identified, security issue.

## Recommendation
Pass the command as a list of arguments and drop `shell=True`, so input can't add shell syntax. Put `--` before a value that could start with `-`.

## Acceptance criteria
- Address: subprocess_popen_with_shell_equals_true
- Keep the change scoped to the cited file/symbols unless impact requires otherwise
- Pass the command as a list of arguments and drop `shell=True`, so input can't add shell syntax. Put `--` before a value that could start with `-`.
- Re-check `orders_api/health.py` after the fix

## Evidence
```
4 def ping(host: str) -> int:
5     return subprocess.call("ping -c1 " + host, shell=True)

```

## Code snippet
```
    1| import subprocess
    2| 
    3| 
    4| def ping(host: str) -> int:
    5|     return subprocess.call("ping -c1 " + host, shell=True)
```

## Verify
- `pytest -x --tb=short -q`

## Done means
- Add or change a test that fails on `dev` and passes with your fix; validate_patch runs it on both trees. Without one the pack becomes `unverified`. If no test can exercise the behaviour, pass `evidence_waiver` with the reason.

Do not auto-merge. Prefer minimal patches with citations.
