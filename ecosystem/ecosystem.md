> Canonical ecosystem reference for the Argus suite. If another repo needs this document, link to this file rather than maintaining a divergent copy.

## Repos (scanned)

Night conductor scans `dev` on every `smk762/argus-*` repo, plus shared dependencies the suite consumes:

| Area | Repo | Role |
|------|------|------|
| Taxonomy / AI contract | [argus-cortex](https://github.com/smk762/argus-cortex) | Shared foundation: taxonomy, versioned wire schemas, local/remote AI backend contract |
| Acquisition | [argus-quarry](https://github.com/smk762/argus-quarry) | Provenance-first public-domain / CC0 portrait acquisition (input stage) |
| Curation | [argus-curator](https://github.com/smk762/argus-curator) | Dataset curation for LoRA training — quality, diversity, subset selection |
| Curation demo | [argus-cull](https://github.com/smk762/argus-cull) | Demo for curator: which images make the training set |
| Captioning | [argus-lens](https://github.com/smk762/argus-lens) | Structured image captioning for training and generation |
| Gallery | [argus-lightbox](https://github.com/smk762/argus-lightbox) | Static gallery navigated by lens captions (facets, EXIF, sidecars) |
| Studio UI | [argus-studio](https://github.com/smk762/argus-studio) | Suite web UI + compose orchestration: curate, caption, export LoRA datasets |
| Guarded gen | [argus-darkroom](https://github.com/smk762/argus-darkroom) | Guarded image generation for the suite |
| Gen demo | [argus-safelight](https://github.com/smk762/argus-safelight) | Demo UI for darkroom with verdicts on show |
| Training bridge | [argus-forge](https://github.com/smk762/argus-forge) | Curated exports → kohya / OneTrainer / diffusers LoRA configs |
| Eval | [argus-proof](https://github.com/smk762/argus-proof) | Post-training LoRA evaluation and optimisation |
| Maps | [argus-atlas](https://github.com/smk762/argus-atlas) | Georeferenced fantasy-map tiles from DEM + cartography LoRA |
| Survey | [argus-survey](https://github.com/smk762/argus-survey) | Point-cloud deliverables for land surveyors (PDAL/GDAL/PROJ) |
| Survey UI | [argus-sightline](https://github.com/smk762/argus-sightline) | Web workspace for survey deliverables |
| Provenance | [argus-mint](https://github.com/smk762/argus-mint) | Provenance tokens for game assets (IPFS, ERC-721, Merkle anchors) |
| Review UI | [argus-assay](https://github.com/smk762/argus-assay) | Review UI for mint — approve cards/templates/frames before pin/sign |
| Face / TTS face | [argus-mask](https://github.com/smk762/argus-mask) | Audio → facial animation frames for LAN screens |
| Infra | [argus-halo](https://github.com/smk762/argus-halo) | Public demo infra (Hetzner, Terraform, Cloudflare) |
| CI | [argus-ci](https://github.com/smk762/argus-ci) | Reusable GitHub Actions for the suite |
| Package template | [argus-pkg-template](https://github.com/smk762/argus-pkg-template) | Copier template for Argus Python packages |
| Chat / RAG | [agent-composer](https://github.com/smk762/agent-composer) | Ollama chat, rag-ingest, Qdrant — consumed by Argus for AI/RAG |
| ComfyUI | [voluptas](https://github.com/smk762/voluptas) | ComfyUI inference node — consumed by Argus generation paths |

**Not scanned (integration only):** [mithrandir](https://github.com/smk762/mithrandir) receives night-conductor status via HTTP (`LATEST.json` / companion events). Do not clone it for audits.

---

## Ecosystem flow

```
  quarry ──► curator / cull ──► lens / lightbox ──► studio
                 │                      │
                 ▼                      ▼
              forge ──► (train) ──► proof
                 │
                 ▼
           darkroom / safelight ──► voluptas (ComfyUI :8188)
                 │
                 ▼
              mint / assay

  atlas (maps)     survey / sightline (point clouds)
  mask (face)      halo (demo infra)

  Shared AI / RAG:  argus-* ──► agent-composer (rag-chat :9150, rag-ingest :9050, Qdrant :6333)
  Night auditor:    ai-code-auditor (Dragon :8765) ──► mithrandir (Jarrah :8400)
```

---

## Deployment notes (current)

Hosts below are placeholders (`<AGENT_HOST>`, `<STATEFUL_HOST>`). The real host/IP is in the local `.env` (see `.env.example`: `AI_AUDIT_PUBLIC_BASE_URL`, `DATABASE_URL`, `OLLAMA_BASE_URL`, `AI_AUDIT_MITHRANDIR_WEBHOOK_URL`); check it there.

| Host | Role |
|------|------|
| Dragon `<AGENT_HOST>` | `ai-code-auditor`, `agent-composer` (rag-chat/rag-ingest), local argus checkouts / mirrors |
| Jarrah `<STATEFUL_HOST>` | Stateful node: Postgres `:5432`, Redis `:6379`, MinIO `:9000`, Qdrant `:6333`, Ollama `:11434`; `mithrandir` `:8400` |
| Voluptas host | ComfyUI `:8188` (see voluptas compose); Argus darkroom/studio call into it |

Branch policy: all `argus-*` repos use **`dev` as default**. Night conductor syncs mirrors and checks out `dev` before each rest session.

---

## Night conductor + mithrandir

| Artifact | URL |
|----------|-----|
| Status envelope | `http://<AGENT_HOST>:8765/reports/mithrandir/LATEST.json` |
| TTS body | `http://<AGENT_HOST>:8765/reports/mithrandir/LATEST.md` |
| Webhook | `POST http://<STATEFUL_HOST>:8400/api/companion/events` |

`LATEST.json` is one `source=ai-code-auditor` status event in mithrandir’s shared envelope — not exclusive owner of the morning broadcast.

---

## RAG

- Collection `audit_docs` (isolated from chat `project_docs`).
- Ingest slim `LATEST.md` + short JSON only (not full SARIF).
- Prefer Jarrah Ollama for embeddings: `http://<STATEFUL_HOST>:11434`.

## Agent coding packs

Loop: **scan → compressed world model → specialist coding packs** (no paste required).

| Layer | Kind examples | Purpose |
|-------|---------------|---------|
| World model | `world_model` (`pack-world-ecosystem`, `pack-world-<repo>`, architecture memory) | Compressed topology / ownership / decisions |
| Specialist | `architecture`, `security`, `bughunt`, `extraction`, `diff_audit`, `nightly` | Actionable coding work items |

| Artifact | Location |
|----------|----------|
| Pack YAML + markdown brief | `reports/agent_packs/<id>.yaml` / `.md` |
| Discovery index | `reports/agent_packs/INDEX.json` (`layers`, `kind_counts`) |
| World model | `GET http://<AGENT_HOST>:8765/audit/packs?kind=world_model` |
| Specialists | `GET /audit/packs?layer=specialist&repo=&q=` |
| **Mission bundle** | `GET /audit/packs/bundle?repo=<name>&token_budget=2500&limit_specialists=5` (world model + ranked specialists + capped `briefs_markdown`) |
| Full pack / brief | `GET /audit/packs/{id}` · `/brief` |
| Status lifecycle | `POST /audit/packs/{id}/status` `{status, note?}` — `open → claimed → in_progress → resolved \| wontfix`; filter with `?status=` |
| CLI | `ai-audit packs --bundle --repo <name>` · `--kind world_model` · `--layer specialist` · `--status open` |
| Refresh | `python scripts/emit_agent_packs.py` |

Ecosystem audit, nightly audit, night conductor, and `POST /audit/diff` emit packs; slim summaries (with `layer`, `pack_kind`, `status` metadata) land in `audit_docs` by default (`AI_AUDIT_PACK_RAG_INGEST=0` or `--no-rag` to skip). Merges to `dev` are picked up by an **hourly GitHub API poll** (`ai-audit merge-poll` / `ai-audit-merge-poll.timer`) that enqueues a lightweight single-repo refresh when a tip SHA moves (global gap between repos — see `contrib/github/merge-refresh-webhook.md`); the overnight timers remain the full-suite source of truth. Agents call the bundle once, claim a pack, then `POST /audit/validate_patch` with `pack_id` — a passing run marks the pack `resolved`; a conductor draft PR marks its pack `in_progress` with `repair.draft_pr_url`.

---

## Architecture governance (summary)

See [`config/architecture_rules.yaml`](../config/architecture_rules.yaml):

- `argus-cortex` owns shared taxonomy / wire schemas.
- Generation workers call `voluptas`; chat/RAG goes through `agent-composer`.
- UI demos (`cull`, `safelight`, `lightbox`, `sightline`, `assay`) should not reimplement cortex contracts.
- Night conductor is aware of open PRs/issues on `dev` to avoid duplicate draft PRs.
