# Agent notes — ai-code-auditor

## Coding packs (prefer over pasted context)

Scans emit **agent coding packs**: compressed work items (finding, snippet, constraints, verify commands).

**Find a pack** (do not ask the user to paste):

```bash
ai-audit packs --json
ai-audit packs --repo argus-cortex --q auth
ai-audit packs --id pack-… --brief
```

HTTP (Dragon):

- `GET http://192.168.1.198:8765/audit/packs?repo=&severity=&kind=&q=`
- `GET /audit/packs/{id}` — full pack
- `GET /audit/packs/{id}/brief` — markdown brief

Also: `reports/agent_packs/INDEX.json`, and slim RAG docs in `audit_docs` (`kind=agent_coding_pack`).

After a fix: use pack `suggested_test_commands` or `POST /audit/validate_patch`.

Emit/refresh without a full night run: `python scripts/emit_agent_packs.py`.

See `ecosystem/ecosystem.md` (Agent coding packs) and `CLAUDE.md`.
