# Nightly Repo Report (Example)

## Summary
- Total findings: 24
- Critical: 2
- High: 9
- Medium: 8
- Low: 5

## Highlights
- Critical findings include exposed admin token fallback and permissive CORS config.
- High-severity issues cluster around secret handling and auth guard bypass risks.
- Medium findings show reliability debt: weak retries, broad exception catches, stale TODOs.
- Recommendation trend: harden defaults, standardize auth middleware, and tighten CI gates.

## Category Breakdown
- Security: 14
- Reliability: 5
- Code quality: 3
- Architecture: 2

## Ownership + SLA Snapshot
- `platform-security`: 8 findings (2 critical, 5 high, 1 medium)
- `identity-team`: 7 findings (3 high, 3 medium, 1 low)
- `devex`: 9 findings (4 medium, 4 low, 1 high)
- 72-hour SLA breaches: 1 (critical), escalated to incident channel

## Sample Findings

### Potential hardcoded secret
- Severity: HIGH
- Type: security
- Repo: ai-code-auditor
- File: `auditor/llm_client.py`:13
- Source: heuristic
- Description: Matched heuristic rule: hardcoded_secret
- Recommendation: Move secrets to environment variables or a secret manager.

### Admin auth bypass fallback enabled
- Severity: CRITICAL
- Type: security
- Repo: ai-code-auditor
- File: `auditor/auth.py`:88
- Source: llm+heuristic
- Description: Fallback path allows admin access when token parsing fails.
- Recommendation: Remove fallback and fail closed with explicit auth error.

### Wildcard CORS in production profile
- Severity: CRITICAL
- Type: security
- Repo: ai-code-auditor
- File: `auditor/settings.py`:54
- Source: heuristic
- Description: `*` origin configured under production environment branch.
- Recommendation: Restrict allowed origins and enforce environment validation.

### Broad exception swallowing in pipeline checkpoint
- Severity: MEDIUM
- Type: reliability
- Repo: ai-code-auditor
- File: `auditor/persistence.py`:142
- Source: heuristic
- Description: `except Exception` block logs and continues without checkpoint rollback.
- Recommendation: Catch specific exceptions and fail pipeline stage on persistence errors.

### Duplicate request validation logic
- Severity: MEDIUM
- Type: architecture
- Repo: ai-code-auditor
- File: `auditor/repo_resolver.py`:31
- Source: pattern_mining
- Description: Validation branch duplicates logic implemented in `auditor/config.py`.
- Recommendation: Extract shared validator to single utility module.

### Debug print statement present
- Severity: LOW
- Type: code_quality
- Repo: ai-code-auditor
- File: `cli.py`:47
- Source: heuristic
- Description: Matched heuristic rule: debug_print
- Recommendation: Replace debug print with structured logging.

## Reviewer Feedback
- Security lead: "Critical auth fallback must be fixed before next release cut."
- Platform lead: "Validation duplication should be resolved with shared utility extraction."
- DevEx reviewer: "Low-severity noise is acceptable if critical/high trendline drops next run."

## Remediation Progress
- Closed in this cycle: 6
- In progress: 10
- Not started: 8
- Next checkpoint: run `ai-audit repo-run` after auth and CORS fixes land.
