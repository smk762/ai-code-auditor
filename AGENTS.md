# Agent notes — ai-code-auditor

## Loop: scan → compressed world model → specialist coding packs

Do **not** wait for a pasted brief.

### Quick start: mission bundle (one call)

`$AI_AUDIT_PUBLIC_BASE_URL` is the audit API base (`http://<AUDIT_HOST>:8765`). The real host/IP is in the local `.env` (see `.env.example`); check it there.

```bash
ai-audit packs --bundle --repo argus-cortex            # markdown briefs
ai-audit packs --bundle --repo argus-cortex --json
curl -s "$AI_AUDIT_PUBLIC_BASE_URL/audit/packs/bundle?repo=argus-cortex&token_budget=2500&limit_specialists=5"
```

Returns `world_model` (ecosystem + architecture memory + `pack-world-<repo>`), ranked
`specialists`, `briefs_markdown` capped at ~`token_budget` tokens (~4 chars/token), `truncated`,
and `discovery`. Lowest-ranked specialists are dropped first; the ecosystem pack is never dropped.
Optional: `q=<keyword>` boosts matches, `status=open,claimed,in_progress` (default) hides closed packs.

### Preferred consuming-agent flow

1. **Bundle** — `GET /audit/packs/bundle?repo=<name>` (or `ai-audit packs --bundle --repo <name>`).
2. **Claim** — `POST /audit/packs/<id>/status {"status":"claimed"}` for the specialist pack you pick. A **409**
   means another agent already holds it (the body names its status and note): pick another pack. `"force": true`
   takes it over; use it only for your own earlier claim.
3. **Fix** — `ai-audit repair --pack-id <id>` (agent-composer repair loop; fetches the pack and injects
   acceptance criteria / tests / constraints; `--audit-api-base` overrides the API base, but rag-chat accepts only its configured audit API or a host in
   its `AUDIT_API_ALLOWED_HOSTS`), **or** implement
   directly from the pack brief (`GET /audit/packs/<id>/brief`). `--mode review` / `suggest` needs no
   rag-chat credential; `--apply` / `--commit` / `--push` need a rag-chat API key in `RAG_CHAT_API_KEY`
   (or `--rag-chat-api-key`). The ai-audit JWT is never forwarded to rag-chat.
4. **Close** — `POST /audit/validate_patch {"repo","patch","pack_id":"<id>"}`. A pass resolves the pack only with
   evidence (see "Done means" in the brief): for bug and security packs, a test in the patch that **fails on `dev` and
   passes with the fix**; for structural packs (`module_cycle`, `private_import`), the violation gone with none added
   or moved, and the tests run (a waiver can replace the tests, never the structure). Otherwise the pack becomes `unverified` and `pack.reason` says what's missing;
   `"evidence_waiver": "<why no test can exercise it>"` resolves it anyway, recorded on the pack.
   The "fails on `dev`" run uses your patched test files whole (anything under `tests/` too), so a helper defined
   inside a test file runs its fixed version on `dev` as well and can't show the failure. A patch that changes only
   test files resolves as `test_only` without that run; if it also changes code and the proof needs the old helper,
   move the helper out of the test file into code, or send a waiver that says so.
   One patch that fixes several packs: add `"pack_ids": ["<id>", ...]` (up to 25). The suite runs once, each pack
   is judged on its own evidence, and `packs` in the response lists every result. A pack's evidence is only the
   tests whose file names its cited module, whether it is validated alone or with others: another pack's test
   never resolves it. If your test lives elsewhere (it reaches the code through a route, a CLI or an alias),
   name it: `"pack_tests": {"<id>": ["tests/test_x.py::test_y"]}`. An id without `[case]` covers every
   parametrized case, and a file covers its tests. A pack that cites a test file is fixed by changing that file
   and resolves `test_only` once the tests run; no other test's repro counts for it.
   If the fix has already merged (the patch no longer applies to `dev`), send `"base_ref": "<sha>"`, the
   `dev` commit your diff was built on. The patch and the "fails on `dev`" run then both use that commit.
   It must be an ancestor of the `dev` tip (else 400), and the pack records it.
   A **429** means the server's validation slots are full: wait `Retry-After` seconds and send the same
   request again. Don't retry on a client timeout while a validation may still be running; it starts a
   duplicate run.

```bash
B=$AI_AUDIT_PUBLIC_BASE_URL   # real value in local .env
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
`.cursor/rules/agent-coding-packs.mdc` + the write-time guard hook in the untracked `.claude/settings.local.json`).
It writes into the working checkouts at `~/<name>` of the scanned repos, or the `--path DIR` you give, and never into
a scan mirror under `~/mirrors`. Flags: `--dry-run`, `--repo NAME`, `--hook-only` (just the hook), `--no-guard-hook`.
Local files only, no git.

**Write-time guard hook.** In Claude Code, after each edit to a `.py` file the hook runs the suite's proven
deterministic rules on it and reports hits on the lines you changed ("Guard rules flagged …"). Fix the line, or add a
`# noqa: <code>` / `# nosemgrep: <id>` with the reason, as CI would ask. It is advice only: it never claims, resolves or
proves a pack, so `validate_patch` is still the evidence. `ai-audit guard-check --file <path>` runs it by hand.

### Search the wiki and packs

To find where something lives or how a repo does something, search the audit collection (wiki sections and agent packs) before crawling code:

```bash
ai-audit search "where are uploads validated" --repo argus-mint --kind wiki
curl -s "$AI_AUDIT_PUBLIC_BASE_URL/audit/search?q=where+are+uploads+validated&repo=argus-mint&kind=wiki&top_k=5"
```

Each hit has `title`, `url` (the wiki section), `tags` and `text`. Wiki text cites `[path:Lx-Ly]`, so open the cited lines to confirm. `kind=agent_coding_pack` searches the packs instead. Read-only, no auth.

### Status lifecycle (claim → fix → close)

Statuses: `open` (default) · `claimed` · `in_progress` · `unverified` (passed, but without the evidence the pack needs; still open work) · `resolved` · `wontfix` · `obsolete` (closed by pack GC because no scan reports the issue any more; reopens if it's found again).

```bash
curl -X POST "$AI_AUDIT_PUBLIC_BASE_URL/audit/packs/<id>/status" \
  -H 'Content-Type: application/json' -H "X-API-Token: $AI_AUDIT_API_TOKEN" \
  -d '{"status":"claimed","note":"agent X working"}'
ai-audit packs --set-status claimed --id <id> --note "…"   # local, no HTTP
ai-audit packs --status open --layer specialist --json       # filter
```

Closure is automatic where possible: `POST /audit/validate_patch` with `"pack_id": "<id>"` marks
the pack `resolved` when `passed=true` *and* the evidence fits the pack, else `unverified`
(`auditor/evidence.py`: a failing-then-passing test for bug/security packs, tests for other code
changes, nothing extra for docs/config-only patches, or an `evidence_waiver`); a conductor draft PR marks its pack `in_progress` and stores
`repair.draft_pr_url`. Status survives re-emits. Pack docs are slim-ingested to RAG by default
(`AI_AUDIT_PACK_RAG_INGEST=0` / `--no-rag` to disable).

### Commit trailers

End each fix commit with one trailer block: one `Pack: <id>` line per pack the commit fixes, then
`Evidence: <kind>` (what `validate_patch` reported) and `Class: <class>` when the packs have one
(`Waiver: <reason>` if you used `evidence_waiver`). A revert of the commit then counts against
exactly those packs. Get the block and the PR-body table in one call:

```bash
curl "$AI_AUDIT_PUBLIC_BASE_URL/audit/pr-summary?packs=<id>,<id>&evidence=repro"   # {"trailers", "markdown"}
ai-audit pr-summary --pack <id> --trailers --evidence repro                        # same, locally
```

Paste `markdown` into the PR's "Audit packs" section. Don't hide it in an HTML comment: reviewers
read it.

### Class packs (guard tasks)

A `class_guard` pack (`pack-class-<class>`) is one task that eliminates a whole class of finding
(SSRF, path traversal, fail-open…) with a guard: a ruff or semgrep rule, or a shared helper plus a
rule that forces its use. Its children are the open findings of that class
(`governance.class_parent`), and it ranks first in a repo's bundle. Prefer it over fixing those
children one by one.

1. Land the guard in one repo with a **proof test** and two fixtures: the test asserts the guard
   fires on the positive fixture and is silent on the negative one.
2. Once it is merged to `dev`, record it:
   `POST /audit/packs/pack-class-<class>/guard {"repo","commit","proof_test","fixtures":[...]}`
   (mutating auth), or `ai-audit packs --guard --id … --repo … --commit … --proof-test … --fixture …`.
   `commit` is the guard commit's full 40-char sha (`git rev-parse`), not a prefix.
3. The nightly run proves it: the proof test must pass on `dev` and must **not** pass on the tree
   just before the guard commit. Then it closes that repo's children that the next scan no longer
   reports. Any still reported are marked `class_exception` and stay open as ordinary work.

### Manual path

Fetch packs in this order:

1. **World model** (topology / ownership / durable decisions)
2. **Specialist pack** for the concrete fix or feature

### 1. World model

```bash
ai-audit packs --kind world_model --json
# or
curl -s "$AI_AUDIT_PUBLIC_BASE_URL/audit/packs?kind=world_model"
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
