# Repair pipeline — quickstart

End-to-end reference for the Phase 1 repair workflow:
diff → LLM review/fix → test validation → git branch → commit → push.

---

## Prerequisites

```bash
cd ~/ai-code-auditor
source .venv/bin/activate

# Both services must be running
curl -s http://127.0.0.1:8765/audit/health   # audit API
curl -s http://127.0.0.1:9150/api/repair/jobs/x  # rag-chat (expect 404, not connection error)
```

---

## 1 — Find repos with changes worth repairing

```bash
# Shows every enabled repo: branch, clean/dirty, diff stat vs base branch
curl -s http://127.0.0.1:8765/audit/repos/status | python3 -m json.tool

# Filter to repos that have a diff
curl -s http://127.0.0.1:8765/audit/repos/status \
  | python3 -c "
import json, sys
for r in json.load(sys.stdin):
    if r['has_diff']:
        print(f\"{r['name']:25s}  {r['branch']} → {r['base_branch']}  \
{r['files_changed']}f +{r['insertions']} -{r['deletions']}\")
"
```

`diff_error` is non-empty when the base branch ref isn't in local refs (common on
SSHFS clones where `git fetch` hasn't been run).

---

## 2 — Review mode (no side effects)

The model annotates the diff and proposes a corrected patch. Nothing is applied.

```bash
# From a branch comparison
ai-audit repair \
  --repo kimini-api \
  --compare-branch main \
  --mode review

# From a saved diff file
ai-audit repair \
  --repo kimini-api \
  --diff /tmp/my.diff \
  --mode review

# With a critic pass (second model reviews the proposed patch)
ai-audit repair \
  --repo kimini-api \
  --diff /tmp/my.diff \
  --mode review \
  --critique-model qwen2.5-coder:32b \
  --max-iterations 3

# Suggest-only (no patch produced, just recommendations)
ai-audit repair \
  --repo kimini-api \
  --diff /tmp/my.diff \
  --mode suggest

# Skip test-suite validation (faster; static diff-audit only)
ai-audit repair \
  --repo kimini-api \
  --compare-branch main \
  --mode review \
  --no-validate-tests
```

---

## 2b — Auto-fix + commit (full git workflow via CLI)

Applies the patch, creates a repair branch, and commits.  Add `--push` to also
push.  All git operations call the audit API endpoints — the CLI never touches
git directly.

```bash
# Apply + commit (no push)
ai-audit repair \
  --repo agent-composer \
  --compare-branch dev \
  --mode auto_fix \
  --apply \
  --commit \
  --base-branch dev

# Apply + commit + push
ai-audit repair \
  --repo agent-composer \
  --compare-branch dev \
  --mode auto_fix \
  --apply \
  --commit \
  --push \
  --base-branch dev \
  --remote origin

# Custom commit author
ai-audit repair \
  --repo agent-composer \
  --compare-branch dev \
  --mode auto_fix \
  --apply \
  --commit \
  --author-name "AI Repair Bot" \
  --author-email "repair@example.com"
```

Output when git ops succeed:

```text
[complete]
  Applied:       True
  Branch:        repair/a1b2c3d4e5f6
  Commit:        a1b2c3d4e5f6
  Pushed to:     git@github.com:org/repo.git
```

---

## 3 — Get a raw diff (for scripting or inspection)

```bash
# Returns the raw unified diff between a branch and HEAD
curl -s -X POST http://127.0.0.1:8765/audit/git/diff \
  -H "Content-Type: application/json" \
  -d '{"repo": "kimini-api", "compare_branch": "main"}' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d['error']:
    print('ERROR:', d['error'], '  reason:', d['reason'])
elif d['is_empty']:
    print('No diff — branches are identical')
else:
    print(d['diff'][:2000])
"
```

`reason` values: `repo_not_found`, `branch_not_found`, `path_error`, `git_error`, `timeout`.
A `branch_not_found` error means the branch isn't in local refs — run
`git fetch origin` in the repo, or check `GET /audit/repos/status` for the
configured base branch.

---

## 4 — Validate a patch without applying it

Copies the repo to a temp directory, applies the patch, runs the test/lint suite.
Infra failures (broken venv symlinks, missing runner) are logged as `WARNING` in
the audit-api container logs but do **not** fail the response — check
`failure_summary` for actionable test failures only.

```bash
curl -s -X POST http://127.0.0.1:8765/audit/validate_patch \
  -H "Content-Type: application/json" \
  -d "{
    \"repo\":      \"kimini-api\",
    \"patch\":     $(python3 -c 'import json,sys; print(json.dumps(open("/tmp/my.diff").read()))'),
    \"timeout_s\": 120
  }" | python3 -m json.tool
```

Key response fields:

| Field | Meaning |
|---|---|
| `patch_applied` | `git apply` succeeded on the temp copy |
| `passed` | patch applied AND test suite passed |
| `failure_summary` | ≤20 compact lines for the repair model (pytest FAILED/E- lines, go `--- FAIL:`, etc.) |
| `skipped_reason` | non-empty when validation was skipped (read-only mount, no test runner) |

---

## 5 — Git endpoints (branch, commit, push)

These operate on the live repo. Only writable repos support them — SSHFS repos
with a read-only `.git` will return 409.

```bash
# Create a repair branch (repair/<slug> or repair/<slug>-N on collision)
curl -s -X POST http://127.0.0.1:8765/audit/git/branch \
  -H "Content-Type: application/json" \
  -d '{"repo": "agent-composer", "slug": "fix-auth-middleware", "base_branch": "dev"}' \
  | python3 -m json.tool

# Stage patch-touched files and commit
curl -s -X POST http://127.0.0.1:8765/audit/git/commit \
  -H "Content-Type: application/json" \
  -d "{
    \"repo\":    \"agent-composer\",
    \"message\": \"repair: fix-auth-middleware\n\nAuto-generated by ai-code-auditor.\",
    \"patch\":   $(python3 -c 'import json,sys; print(json.dumps(open("/tmp/my.diff").read()))')
  }" | python3 -m json.tool

# Push (returns pushed:false + skipped_reason for local-path remotes — not an error)
curl -s -X POST http://127.0.0.1:8765/audit/git/push \
  -H "Content-Type: application/json" \
  -d '{"repo": "agent-composer", "branch": "repair/fix-auth-middleware", "remote": "origin"}' \
  | python3 -m json.tool
```

---

## 6 — Full end-to-end: auto_fix + test validation + git ops

The CLI does not yet expose `validate_tests` or `git_ops`, so call rag-chat directly.
The response is an SSE stream; pipe through the filter below to watch progress:

```bash
curl -s -N -X POST http://127.0.0.1:9150/api/repair/run \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "repo":           "agent-composer",
    "compare_branch": "dev",
    "mode":           "auto_fix",
    "apply":          true,
    "validate_tests": true,
    "max_iterations": 3,
    "audit_api_url":  "http://host.docker.internal:8765",
    "git_ops": {
      "enabled": true,
      "commit":  true,
      "push":    false
    }
  }' \
  | grep '^data:' \
  | while IFS= read -r line; do
      echo "${line#data: }" \
        | python3 -m json.tool --no-ensure-ascii 2>/dev/null \
        | grep -E '"iteration|validation_passed|test_validation_passed|branch|commit_sha|applied|error"'
      echo "---"
    done
```

The `complete` event carries:

```json
{
  "applied":    true,
  "branch":     "repair/...",
  "commit_sha": "abc123...",
  "push_url":   "",
  "git_error":  ""
}
```

`git_error` is non-empty only for non-fatal git failures (e.g. push skipped, commit
empty). `applied: true` with a non-empty `git_error` means the patch was written to
disk but the commit/push step had an issue.

---

## 7 — Retrieve a completed job

```bash
JOB_ID="repair-42ff310de3df39a1"

curl -s http://127.0.0.1:9150/api/repair/jobs/$JOB_ID | python3 -m json.tool
```

---

## 8 — Monitor container logs for infra warnings

Broken venv symlinks, missing test runners, and other infrastructure issues are
suppressed from the repair model but logged as `WARNING` in the audit-api container:

```bash
docker logs -f audit-api 2>&1 | grep -E "WARNING|validate_patch|infra|git"
```

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `Audit API unreachable` | rag-chat can't reach `host.docker.internal:8765` | Add `extra_hosts: ["host.docker.internal:host-gateway"]` to rag-chat in `docker-compose.yml` and set `AUDIT_API_URL=http://host.docker.internal:8765` |
| `Branch 'X' not found in local refs` | Branch hasn't been fetched locally | Run `git fetch origin` in the repo, or use the branch name shown by `GET /audit/repos/status` |
| `409` on git/branch | Repo `.git` is read-only (SSHFS mount) | Git ops only work on locally writable repos |
| `patch_applied: false` | Diff is stale (already merged) or wrong base | Use `POST /audit/git/diff` to get a fresh diff |
| `failure_summary: []`, `passed: false` | Infra issue (broken venv, no runner) | Check `docker logs audit-api` for `WARNING validate_patch` lines |
