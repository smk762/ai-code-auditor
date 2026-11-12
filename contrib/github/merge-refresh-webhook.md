# Merge-to-dev pack / RAG refresh

Nightly timers keep the full ecosystem world model fresh. After merges to **`dev`**,
a lightweight path keeps agent packs + `audit_docs` from going stale.

## Recommended: hourly GitHub API poll (no inbound webhook)

GitHub cannot reach a LAN IP. Instead, poll each mirror’s `dev` tip every hour and
enqueue a refresh when the SHA moves:

```bash
# One-shot (primes state on first run — no stampede)
ai-audit merge-poll --baseline-only

# Detect changes and POST into the running audit-api worker
ai-audit merge-poll --schedule

# systemd (installed by scripts/install_overnight_timers.sh)
systemctl --user list-timers | grep merge-poll
```

| Env | Default | Meaning |
|-----|---------|---------|
| `AI_AUDIT_MERGE_POLL_ENABLED` | `1` | Master switch for the poller |
| `AI_AUDIT_MERGE_POLL_STATE` | `reports/merge_poll_state.json` | Last-seen SHAs |
| `GITHUB_TOKEN` / `GH_TOKEN` | _(empty)_ | **Required** for private repos + to avoid the 60 req/hr anonymous cap |
| `AI_AUDIT_MERGE_BRANCH` | `dev` | Branch tip to watch |

First sighting of a repo only **baselines** the tip (no refresh). Later SHA moves
enqueue `POST /audit/merge` with `before`/`after` and `force=true` (still serialised
with `AI_AUDIT_MERGE_GLOBAL_GAP_S` between repos).

## Endpoints (still available)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/audit/merge` | Explicit schedule `{repo, before?, after?, force?}` |
| `GET`  | `/audit/merge` | Pending / running / recent jobs |
| `POST` | `/audit/hooks/github` | Optional push webhook if you later expose a tunnel |

## Debounce / burst control

| Env | Default | Meaning |
|-----|---------|---------|
| `AI_AUDIT_MERGE_HOOK_ENABLED` | `1` | Refresh worker switch |
| `AI_AUDIT_MERGE_DEBOUNCE_S` | `900` | Per-repo quiet window (webhook/manual) |
| `AI_AUDIT_MERGE_GLOBAL_GAP_S` | `120` | Pause between different repos in a burst |
| `AI_AUDIT_MERGE_WEBHOOK_SECRET` | _(empty)_ | Only needed for `/audit/hooks/github` |

## Optional webhook (tunnel / public URL only)

Prefer the hourly poll on LAN. If you expose audit-api publicly:

1. Repo → Settings → Webhooks → Add webhook  
2. Payload URL: your **public** HTTPS URL + `/audit/hooks/github`  
3. Secret: `AI_AUDIT_MERGE_WEBHOOK_SECRET`  
4. Events: **Just the push event**

## CLI

```bash
ai-audit merge-refresh --repo argus-cortex --now       # immediate local refresh
ai-audit merge-refresh --repo argus-cortex --schedule  # one repo via API
ai-audit merge-poll --schedule                         # all mirrors via API
curl -s http://127.0.0.1:8765/audit/merge | jq
```

## What a refresh does (lightweight)

1. Sync that repo’s local mirror / checkout `dev`  
2. Diff-audit `before..after`  
3. Emit specialist packs + `pack-world-<repo>`  
4. Slim-ingest to `audit_docs` when `AI_AUDIT_PACK_RAG_INGEST` is on  

It does **not** re-run the full ecosystem audit (that stays on the 02:00 timer).
