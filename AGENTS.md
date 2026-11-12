# Agent notes — ai-code-auditor

## Loop: scan → compressed world model → specialist coding packs

Do **not** wait for a pasted brief. Fetch packs in this order:

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
- `GET /audit/packs?layer=specialist&repo=…&q=…`
- `GET /audit/packs/{id}` / `/brief`

On disk: `reports/agent_packs/INDEX.json` (includes `layers` + `kind_counts`).

After a fix: pack `suggested_test_commands` or `POST /audit/validate_patch`.

Refresh full loop: `python scripts/emit_agent_packs.py`

See `.cursor/rules/agent-coding-packs.mdc`, `ecosystem/ecosystem.md`, `CLAUDE.md`.
