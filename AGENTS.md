# Agent notes — ai-code-auditor

## Loop: scan → compressed world model → specialist coding packs

Do **not** wait for a pasted brief.

### Quick start: mission bundle (one call)

```bash
ai-audit packs --bundle --repo argus-cortex            # markdown briefs
ai-audit packs --bundle --repo argus-cortex --json
curl -s 'http://192.168.1.198:8765/audit/packs/bundle?repo=argus-cortex&token_budget=2500&limit_specialists=5'
```

Returns `world_model` (ecosystem + architecture memory + `pack-world-<repo>`), ranked
`specialists`, `briefs_markdown` capped at ~`token_budget` tokens (~4 chars/token), `truncated`,
and `discovery`. Lowest-ranked specialists are dropped first; the ecosystem pack is never dropped.
Optional: `q=<keyword>` boosts matches, `status=open,claimed,in_progress` (default) hides closed packs.

### Preferred consuming-agent flow

1. **Bundle** — `GET /audit/packs/bundle?repo=<name>` (or `ai-audit packs --bundle --repo <name>`).
2. **Claim** — `POST /audit/packs/<id>/status {"status":"claimed"}` for the specialist pack you pick.
3. **Fix** — `ai-audit repair --pack-id <id>` (agent-composer repair loop; fetches the pack and injects
   acceptance criteria / tests / constraints; `--audit-api-base` overrides the API base), **or** implement
   directly from the pack brief (`GET /audit/packs/<id>/brief`).
4. **Close** — `POST /audit/validate_patch {"repo","patch","pack_id":"<id>"}`; `passed=true` marks the pack `resolved`.

```bash
B=http://192.168.1.198:8765
curl -s "$B/audit/packs/bundle?repo=argus-cortex&token_budget=2500"                      # 1
curl -X POST "$B/audit/packs/<id>/status" -H 'Content-Type: application/json' \
  -H "X-API-Token: $AI_AUDIT_API_TOKEN" -d '{"status":"claimed","note":"agent X"}'       # 2
ai-audit repair --repo argus-cortex --pack-id <id> --mode review                        # 3
curl -X POST "$B/audit/validate_patch" -H 'Content-Type: application/json' \
  -H "X-API-Token: $AI_AUDIT_API_TOKEN" \
  -d '{"repo":"argus-cortex","patch":"<unified diff>","pack_id":"<id>"}'                 # 4
```

Give up on a pack with `status: wontfix` (+ `note`). Other repos get this flow via
`bash scripts/install_agent_pack_awareness.sh` (idempotent `AGENTS.md` section +
`.cursor/rules/agent-coding-packs.mdc`; `--dry-run`, `--repo NAME`; local files only, no git).

### Status lifecycle (claim → fix → close)

Statuses: `open` (default) · `claimed` · `in_progress` · `resolved` · `wontfix`.

```bash
curl -X POST 'http://192.168.1.198:8765/audit/packs/<id>/status' \
  -H 'Content-Type: application/json' -H "X-API-Token: $AI_AUDIT_API_TOKEN" \
  -d '{"status":"claimed","note":"agent X working"}'
ai-audit packs --set-status claimed --id <id> --note "…"   # local, no HTTP
ai-audit packs --status open --layer specialist --json       # filter
```

Closure is automatic where possible: `POST /audit/validate_patch` with `"pack_id": "<id>"` marks
the pack `resolved` when `passed=true`; a conductor draft PR marks its pack `in_progress` and stores
`repair.draft_pr_url`. Status survives re-emits. Pack docs are slim-ingested to RAG by default
(`AI_AUDIT_PACK_RAG_INGEST=0` / `--no-rag` to disable).

### Manual path

Fetch packs in this order:

1. **World model** (topology / ownership / durable decisions)
2. **Specialist pack** for the concrete fix or feature

### 1. World model

```bash
ai-audit packs --kind world_model --json
# or
curl -s 'http://192.168.1.198:8765/audit/packs?kind=world_model'
```

Always useful ids:

- `pack-world-ecosystem` — suite map, pipeline, ownership
- `pack-world-architecture-memory` — durable decisions
- `pack-world-<repo>` — per-repo briefing (deps, callers, endpoints, security notes)

### 2. Specialist coding packs

```bash
ai-audit packs --layer specialist --repo argus-cortex --json
ai-audit packs --kind architecture --json
ai-audit packs --kind extraction --json
ai-audit packs --id <pack-id> --brief
```

Kinds: `architecture`, `security`, `bughunt`, `nightly`, `diff_audit`, `extraction`, …

HTTP:

- `GET /audit/packs?layer=world_model`
- `GET /audit/packs?layer=specialist&repo=…&q=…&status=open`
- `GET /audit/packs/bundle?repo=…&token_budget=2500`
- `POST /audit/packs/{id}/status` `{status, note?}`
- `GET /audit/packs/{id}` / `/brief`

On disk: `reports/agent_packs/INDEX.json` (includes `layers`, `kind_counts`, `status_counts`).

After a fix: pack `suggested_test_commands` or `POST /audit/validate_patch` (include `pack_id`).

Refresh full loop: `python scripts/emit_agent_packs.py`

### Merge-to-dev refresh (lightweight)

Nightly timers own the full world model. On LAN, an **hourly GitHub API poll**
detects `dev` tip moves and enqueues single-repo pack/RAG refreshes (2m gap
between repos in a burst — no inbound webhook required):

```bash
ai-audit merge-poll --baseline-only          # first run: record tips only
ai-audit merge-poll --schedule               # hourly via systemd timer
ai-audit merge-refresh --repo argus-cortex --schedule
# details: contrib/github/merge-refresh-webhook.md
```

See `.cursor/rules/agent-coding-packs.mdc`, `ecosystem/ecosystem.md`, `CLAUDE.md`.
