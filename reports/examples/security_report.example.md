# Security Report (Example)

- Security findings: 16
- Critical: 3
- High: 8
- Medium: 5

## Findings by Class
- Secrets exposure: 7
- Authentication/authorization: 4
- Dependency/supply-chain: 2
- Configuration hardening: 3

## Sample Entries
- [HIGH] ai-code-auditor `auditor/llm_client.py`:13 - Potential hardcoded secret
- [HIGH] ai-code-auditor `auditor/settings.py`:23 - Potential hardcoded secret
- [CRITICAL] ai-code-auditor `auditor/auth.py`:88 - Admin auth bypass fallback path
- [CRITICAL] ai-code-auditor `auditor/settings.py`:54 - Wildcard CORS enabled in production profile
- [HIGH] ai-code-auditor `auditor/static_tools.py`:52 - Semgrep `--config=auto` may resolve remote rule bundles
- [MEDIUM] ai-code-auditor `auditor/repo_resolver.py`:21 - Missing role check on privileged path
- [MEDIUM] ai-code-auditor `pipelines/nightly_repo_audit.py`:72 - Unverified dependency integrity metadata
- [CRITICAL] ai-code-auditor `auditor/archive.py`:11 - Token may leak via verbose error logs

## Suggested Remediation Playbook
1. Replace hardcoded literals with environment-backed configuration.
2. Rotate any exposed credentials immediately.
3. Enforce fail-closed auth behavior; remove bypass/fallback code paths.
4. Restrict CORS/headers to approved origins in production.
5. Add pre-commit secret scanning and block high-severity findings in CI.
6. Pin and verify dependency integrity for pipeline/runtime packages.

## Team Feedback + Decisions
- Security engineer: "Block release while critical auth/CORS findings remain open."
- SRE: "Prioritize log-token redaction before next on-call rotation."
- Application team: "Role-check gaps are medium risk but easy to patch this sprint."

## Verification Plan
- Re-run `ai-audit repo-run` after remediation PRs merge.
- Validate no critical findings remain and high findings trend down.
- Add regression tests for auth fallback, CORS config, and log redaction.
