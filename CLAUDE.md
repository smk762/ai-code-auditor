# CLAUDE.md

Guidance for coding agents in this repo. It holds the rules and the facts the code doesn't make obvious. For how a
subsystem works, read its module docstring (named below); for the pack consuming flow, read [AGENTS.md](AGENTS.md).

## Must never happen

Each line names what enforces it. An **UNENFORCED** line is a known gap: rely on review, and close it with a check
rather than more prose. When a fix you hand over is rejected, or something slips through, add the missing check
here and in code. Don't add a rule to remember.

| Never | Enforced by |
|---|---|
| Write to a repo mirror (validation, scans and the API only read them) | `:ro` mounts in `docker-compose.yml`; `test_validate_base::test_fetches_code_merged_after_the_nightly_sync_without_touching_the_mirror` |
| Mirror sync discarding a unique local commit | `test_mirror_sync::test_unique_local_commit_is_never_reset` |
| Resolve a pack without evidence (lint-only, or a bug/security fix with no test that fails on `dev` and passes patched) | `test_evidence::test_decide`; the seeded-defect eval's control gate (`auditor/eval_seeded.py`) |
| Report a TS/JS change as passed when no check ran on it | `test_validate_node::test_missing_npm_skips_instead_of_passing`, `::test_failed_install_skips_instead_of_passing` |
| Publish a wiki page that failed the opsec gate, redact one instead of refusing it, or export without the deny-list | `test_wiki_public::test_gate_refuses`, `::test_export_refuses_to_run_without_denylist`, `::test_publish_refuses_tampered_or_leaky_bundles` |
| "Notable findings" or internal INDEX data in the public bundle | `test_wiki_public::test_export_bundle` |
| Keep a wiki claim whose citation is missing, out of range, or names nothing in the cited lines | `test_wiki::test_drop_failed_claims` |
| Commit the opsec deny-list, the classes gold set, or anything quoting real findings | Location only: they live under `~/.config/ai-code-auditor/`, and `reports/_classes/` is gitignored. **UNENFORCED** for a copy placed elsewhere |
| Pack GC closing a claimed, in-progress or unverified pack, or a mass close without `--force` | `test_pack_gc::test_only_open_finding_packs_are_considered`, `::test_mass_closure_is_held_unless_forced`, `::test_broken_latest_scan_skips_the_repo_but_not_file_gone` |
| Write a finding class onto a pack without all three models agreeing, or with an unmeasured prompt | `test_pack_classes::test_consensus_needs_every_model`, `::test_no_frozen_prompt_skips` |
| A gate passing below its minimum sample | `test_trust_eval::test_failing_and_insufficient`, `::test_stale_or_missing_seeded_eval_is_insufficient` |
| Overwrite `trust_history.jsonl`, `trust_eval_history.jsonl` or `pack_savings_history.jsonl` | `test_trust_metrics::test_refresh_writes_report_and_appends_history`, `test_trust_eval::test_evaluate_writes_report_and_history`, `test_pack_savings::test_refresh_writes_report_and_appends_history` |
| A partial RAG send retiring other docs | `test_rag_retire::test_sync_retires_only_what_the_send_replaced`, `::test_failed_delete_is_retried_and_failed_send_retires_nothing` |
| Remove a GitHub label this tool didn't add, or label an external item | `test_linchpin::test_apply_labels_adds_removes_only_its_own_and_records_ledger` |
| Spend past a lane budget | `test_spend_ledger::test_lane_and_total_caps`, `::test_batch_prefetch_trims_to_budget` |
| Serve a brand SVG with scripts or event handlers | `test_ui_topology::test_mark_rejects_active_content` |
| A mutating endpoint without `_require_mutating_auth` | Per endpoint (`test_code_review_high_fixes::test_mutating_api_requires_token_when_configured` and others). **UNENFORCED** for a new endpoint: no test sweeps the routes |
| Work pushed to `main`, a PR into `main` other than the release, or `main` ahead of `dev` except the release merge commit | **UNENFORCED** (see Branches) |
| Deploy hooks in dragonhound-site, or docs committed into it | **UNENFORCED** here (another repo). Docs go live only by a `docs.lock.json` pin bump on its `dev` |

## Setup and commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]            # ai-audit CLI; .[semantic] adds faiss-cpu
alembic upgrade head             # audit DB (Postgres or SQLite)
python -m pytest tests/ -v

docker compose up -d --build audit-api        # API on :8765
docker compose run --rm --no-deps auditor     # one-off ecosystem audit
bash scripts/install_overnight_timers.sh      # rest 00:30 + ecosystem 02:00 user timers
```

`ai-audit --help` lists every command. The ones agents use most: `packs --bundle --repo <r>`, `packs --id <id> --brief`,
`packs --set-status <s> --id <id>`, `search "q" --repo <r> --kind wiki`, `pr-summary --pack <id> --trailers`,
`eval trust`, `trust`, `pr-intel`, `wiki build --dry-run` (prices a build, no API calls).

Config lives in `config/*.yaml`. Every key in `auditor/config.py` can be overridden by an `AI_AUDIT_*` env var; copy
`.env.example` to `.env`. `AI_AUDIT_PUBLIC_BASE_URL` and the real hosts are only in the local `.env`.

## Facts the code doesn't make obvious

**Mirrors.** The scan set is every `smk762/argus-*` plus `agent-composer` and `voluptas`, under `/home/smk/<name>` or
`/home/smk/mirrors/<name>` (`config/repo_mirrors.yaml`). `ai-audit rest-run` syncs them to `dev` first, and
`scripts/sync_repo_mirrors.sh` does it by hand. A mirror is fast-forwarded, or reset with a backup branch after an
upstream rewrite, and a mirror with a unique local commit is never touched (`conductor/mirrors.py`). Mirrors only sync nightly, so `validate_patch` fetches `dev` itself (`auditor/validate_base.py`). SSHFS mounts
are deprecated. `../argus-core` is a symlink alias for the cortex mirror: suite repos import `@../argus-core/SUITE.md`
on purpose. Topology: [`ecosystem/ecosystem.md`](ecosystem/ecosystem.md).

**Auth.** Read-only pages and JSON need no auth. Every action goes through `_require_mutating_auth`: on this host,
`AI_AUDIT_API_TOKEN` as `X-API-Token`. The same value is in agent-composer's `.env` and in the `env` block of
`~/.claude/settings.json`, so coding agents in any repo have it.

**Findings and runs.** Only repo audits store findings rows; ecosystem runs store none, so they never count as scans
(trust metrics, GC, the findings trend). Findings match on repo, file, type and title, never line, and LLM rewordings in
the same file are paired by title word overlap (`auditor/finding_match.py`). Pack dedupe uses the same match.

**The scan's LLM prompt** is `AUDIT_INSTRUCTIONS_V2` in `auditor/llm_client.py`, chosen by the 2026-11-13 eval.
`AI_AUDIT_LLM_PROMPT=v3` is opt-in until measured. `repo_review` in `config/prompts.yaml` is **not** what the scan uses.

**validate_patch** (`auditor/validator.py`, `validate_patch_tree`) applies the patch to a clean `git archive` of the
`dev` tip, never the live checkout. Each changed file maps to its nearest project: for `.py`, a dir with
`pyproject.toml`, `setup.py`, `requirements.txt` or `tests/`; for TS/JS, the nearest `package.json`
(`auditor/validate_node.py`). That project's tests or npm checks then run in a cached env built from its declared deps.
Failures are compared against the unpatched tree, so only failures the patch introduced count. When nothing can run,
the result is `lint_only` (Python) or skipped (TS/JS), with a note saying why. A hatch-vcs or setuptools-scm version
module gets a stub. The response names its base (`base_source`, `base_stale`, `behind_by`).

**Evidence bar** (`auditor/evidence.py`, #58). A pass resolves a pack only with evidence, else it becomes
`unverified`, which is still open work. Bug, security and untyped finding packs need a test from the patch that fails
on `dev` and passes patched. Other code changes need tests or npm checks to have run. Docs/config-only patches pass.
`evidence_waiver` (≥ 20 chars) resolves anyway and is recorded. Fix commits carry `Pack:` / `Evidence:` trailers
(`auditor/trailers.py`), which the trust metrics read from `git log`.

**Trust** (epic #64). `auditor/trust_metrics.py` measures whether agent fixes can be trusted, and every trust-upgrade
change is judged against it. `auditor/trust_eval.py` defines the gates: below its minimum sample a gate is
`insufficient`, never a pass. The exit criteria are every non-pending gate passing in each of the last two ISO weeks,
with headline metrics not worse over 4 weekly snapshots. `auditor/eval_seeded.py` tests the evidence bar against
planted bugs in real mirrors. These run nightly after the repo audit, in this order: pack GC, pack classes, class
packs, trust metrics, gates.

**Finding classes** (#53, `auditor/finding_class.py`, `config/finding_classes.yaml`). The gold set quotes real
findings, so it lives outside the repo in `~/.config/ai-code-auditor/eval/` (`AI_AUDIT_EVAL_DIR`), and tests point that
at an empty dir. Only the blind-labelled `test` set counts toward the gates (precision ≥ 95%, coverage ≥ 80%). As of
2026-11-16 on that set: title rules 87.5% / 32%, the three models agreeing 95.3% / 86%. The council relabelling the gold
set is made of the models being graded, so read scores as agreement, not ground truth. A class is written onto a pack
only when all three models agree, the class has a guard, and the frozen prompt in `reports/_classes/` exists
(`auditor/pack_classes.py`). One class-guard pack per guardable class closes its children once a guard is proven
(`auditor/class_packs.py`).

**Pack GC** (`auditor/pack_gc.py`) is the only thing besides an agent or a person that closes finding packs. It only
closes `open` packs, as `obsolete`, which means "no longer reported", not a fix. A re-found issue reopens the same pack.

**Night page.** Mission outcomes come from the mission files, because mithrandir's worker stats don't count `PARTIAL`.
Spend comes from the ledger's `conductor` lane, because `cost_usd_est` is not filled in.

**Ollama healthcheck** hits `/api/tags` (no model load), not generate.

**Web UI.** Static pages in `auditor/static/` share `ui.css` and `ui.js`, and put `<!--#nav-->` where the server
injects the nav. There is no build step, and each page has its own JSON endpoint. Markdown goes through
`render_markdown()` (raw HTML escaped), the same renderer the public wiki export uses.

## Wiki and public docs

The pipeline is notes (Haiku), then drafts and a check-and-fix pass (Opus), then the citation pass, all through the
Batch API (`wiki/`). Batch ids hash the request, so reruns never pay twice. Every stage is priced against the `wiki`
spend lane first. Only Python/JS/TS sources are read, so YAML, Terraform or Jinja repos get no pages yet.

dragonhound.dev is the only public copy of the docs: there are no GitHub wikis. `wiki/public.py` exports
`argus-*` public repos (a failed visibility lookup counts as private) through the opsec gate. The gate fails closed and
refuses a page outright; a repo with a refused page is left out whole. `wiki/publish.py` commits the bundle to the
private `smk762/argus-docs` as `argus-docs-bot`, only when the bundle hash changed. dragonhound-site deploys by direct
upload and pins docs in `docs.lock.json`; a pin bump on its `dev` is the review gate. `wiki/refresh.py` (Sundays 04:30)
rebuilds only pages whose cited lines changed, putting public pages first when the budget is short.

## RAG

Every sender goes through `auditor/rag_client.py`, which signs requests with `INGEST_SHARED_SECRET`; it must match
agent-composer's. rag-ingest keeps only id, title, url, path and source per doc, so repo and kind go in `tags`. The
collection is `audit_docs`, and `project_docs` is deprecated. Ids sent per scope are recorded in
`reports/rag_ingested.json`, so ids no longer sent can be retired. Resolved and wontfix packs are retired rather than
re-sent, so retrieval shows open work. Search goes through rag-chat's `/search`, so queries are embedded by the model
that wrote the vectors.

## PR intel and linchpins

`ai-audit pr-intel` runs hourly after merge-poll and writes `reports/pr_intel.json` (`/audit/prs`). The linchpin scan
(`auditor/linchpin.py`) marks P0 the root of a cross-repo dependency chain and every member of a cycle. Labelling P0 on
GitHub is the only GitHub write, and it is opt-in (`--label-p0`, or `AI_AUDIT_LINCHPIN_LABELS=1`).

## Packs

Loop: scan → world model → specialist coding packs, in `reports/agent_packs/` (`INDEX.json`). Agents fetch them
themselves; [AGENTS.md](AGENTS.md) has the bundle → claim → fix → `validate_patch` flow.
`python scripts/emit_agent_packs.py` rebuilds world-model, architecture and extraction packs, and leaves finding packs
and their status alone. `bash scripts/install_agent_pack_awareness.sh` writes the flow into every ecosystem repo on disk
(local files only, never git). Re-run it after changing the API base or the flow text.

## Branches

`dev` is the default branch and the base for every PR. Cut feature branches from `origin/dev` and bring them up to date by merging `origin/dev`, never `main`. Never push work to `main` or open a PR against it, except the release PR from `dev`: `main` must never be ahead of `dev`, with one exception. A release fast-forwards `main` to a `dev` commit that passed CI (`git push origin origin/dev:main`), then tags it. If `main` is protected, the release is a reviewed PR from `dev` into `main`, and merging it leaves a merge commit on `main` alone. That merge commit is the one exception to the rule: it leaves `main` one commit ahead of `dev` with an identical tree. Leave it there and don't merge `main` back into `dev`. Anything else on `main` that `dev` lacks is a mistake (`git diff origin/dev origin/main` must be empty). Once `main` carries such a commit it can't be fast-forwarded, so later releases are PRs too.
