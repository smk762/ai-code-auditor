# CLAUDE.md

Guidance for coding agents in this repo. It holds the rules and the facts the code doesn't make obvious. For how a
subsystem works, read its module docstring (named below); for the pack consuming flow, read [AGENTS.md](AGENTS.md).

## Must never happen

Each line names what enforces it. An **UNENFORCED** line is a known gap: rely on review, and close it with a check
rather than more prose. When a fix you hand over is rejected, or something slips through, add the missing check
here and in code. Don't add a rule to remember.

| Never | Enforced by |
|---|---|
| Write to a repo mirror or a configured scan path (validation, scans and the API only read them; draft PRs are built in a throwaway clone) | `:ro` mounts in `docker-compose.yml`; `test_validate_base::test_fetches_code_merged_after_the_nightly_sync_without_touching_the_mirror`; `git_ops.refuse_scan_source` in every branch/commit/push helper (`test_draft_pr_packs::test_git_writes_refuse_a_scan_source`, `::test_every_configured_mirror_and_repo_path_is_a_scan_source`, `::test_draft_pr_never_writes_the_source_checkout`); `test_guard_seeded::test_the_mirror_is_never_written` |
| Mirror sync discarding a unique local commit | `test_mirror_sync::test_unique_local_commit_is_never_reset` |
| Resolve a pack without evidence (lint-only, or a bug/security fix with no test that fails on `dev` and passes patched, or tests that were all skipped) | `test_evidence::test_decide`, `test_evidence_skips::test_a_run_where_nothing_passed_is_unverified`; the seeded-defect eval's control gate (`auditor/eval_seeded.py`) |
| A pack resolved on another pack's test in a multi-pack `validate_patch` | `test_evidence::test_a_pack_never_resolves_on_another_packs_test`, `::test_for_pack_keeps_only_the_tests_naming_the_packs_module` |
| Report a TS/JS change as passed when no check ran on it | `test_validate_node::test_missing_npm_skips_instead_of_passing`, `::test_failed_install_skips_instead_of_passing` |
| Publish a wiki page that failed the opsec gate, redact one instead of refusing it, or export without the deny-list | `test_wiki_public::test_gate_refuses`, `::test_export_refuses_to_run_without_denylist`, `::test_publish_refuses_tampered_or_leaky_bundles` |
| "Notable findings" or internal INDEX data in the public bundle | `test_wiki_public::test_export_bundle` |
| Keep a wiki claim whose citation is missing, out of range, or names nothing in the cited lines | `test_wiki::test_drop_failed_claims` |
| Commit the opsec deny-list, the classes gold set, or anything quoting real findings | Location only: they live under `~/.config/ai-code-auditor/`, and `reports/_classes/` is gitignored. **UNENFORCED** for a copy placed elsewhere |
| An extraction pack without an approval, a decision moving a pack an agent holds, or one resolved on a first passing PR or while a copy can't be read | `test_extraction::test_sync_closes_unapproved_open_packs_and_refreshes_approved_ones`, `::test_a_decision_never_moves_a_pack_an_agent_holds`, `::test_sync_resolves_a_pack_once_every_copy_is_gone_and_never_on_unknown`; `test_evidence::test_a_passing_patch_leaves_an_extraction_pack_in_progress` |
| Pack GC closing a claimed, in-progress or unverified pack, a wontfix pack whose cited code is still there, a live pack because its path can't be anchored to the repo, or a mass close without `--force` | `test_pack_gc::test_only_open_finding_packs_are_considered`, `::test_a_path_that_cannot_be_anchored_is_not_a_deleted_file`, `::test_wontfix_is_kept_while_its_code_is_there`, `::test_mass_closure_is_held_unless_forced`, `::test_broken_latest_scan_skips_the_repo_but_not_file_gone`, `test_pack_write_integrity::test_gc_never_overwrites_a_claim_that_lands_mid_apply`; for architecture packs `test_architecture_packs::test_only_open_architecture_packs_are_closed`, `::test_a_pack_claimed_after_planning_is_left_alone`, `::test_mass_architecture_closure_is_held_unless_forced`, `::test_a_repo_with_an_unread_file_closes_nothing_as_not_reported`, `test_unread_files::test_every_file_a_detector_goes_blind_on_is_listed` |
| A module-cycle pack without an approval that says what to move, a cycle decision moving a pack an agent holds, or a scan that failed or couldn't read the repo closing one, or a cycle staying hidden under a resolved approval that doesn't cover it | `test_structure_packs::test_approval_needs_a_note_and_a_reported_cycle`, `::test_a_decision_never_moves_a_held_pack`, `::test_sync_closes_unapproved_and_gone_packs_but_never_on_unknown`, `::test_nightly_sync_needs_a_scan_that_finished`, `::test_a_cycle_back_after_its_fix_merged_needs_a_fresh_approval`, `::test_a_bigger_cycle_than_a_resolved_approval_is_a_new_proposal` |
| An agent's cycle proposal approving anything, filling the approval note when it is stale or less than high confidence, or being stored when the cycle survives its claimed change | `test_structure_proposals::test_a_proposal_decides_nothing`, `::test_only_a_fresh_confident_fix_is_offered`, `::test_a_proposal_that_leaves_a_circle_is_refused` |
| Read a scan's silence about a file it couldn't review (cache-only miss, over budget, backend down) as the issue being gone: no GC closure, no "gone" or "not refound" in the trust metrics | `test_blind_scans::test_blind_scans_never_close_a_pack_as_not_rescanned`, `::test_blind_scans_dont_confirm_code_changed`, `::test_metrics_ignore_findings_a_blind_scan_could_not_see`, `test_nightly_repo_audit_packs::test_batch_mode_restores_cache_only_after_the_repo_pass` |
| An architecture rule naming a role or repo that doesn't resolve, or one that can match nothing (a silent no-op, including the old extraction approvals' unevaluated `required_imports`/`forbidden_patterns`), or a checked-in rule without a seeded violation it fires on | `config.load_architecture_rules` validates (`ecosystem/arch_contracts.py`); `test_arch_contracts::test_a_rule_that_cannot_resolve_or_match_is_a_load_error`, `::test_every_checked_in_rule_has_a_seeded_fixture`, `test_approved_rules::test_a_checked_in_rule_with_only_unevaluated_keys_is_a_load_error`, `::test_an_old_extraction_approval_is_dropped_not_loaded`; the `arch.contracts` gate |
| A conductor PR above its (repo, class) autonomy level, a change to auth, secrets, migrations or data deletion opened above `draft_pr`, or a scope promoted without a full clean window of evidenced agent PRs and 30 days at its level | `test_autonomy::test_irreversible_changes_never_go_above_draft_pr`, `::test_promotion_needs_a_full_clean_window_evidence_and_a_month`, `::test_any_breach_demotes_one_level`; `test_session_packs::test_a_scope_at_report_gets_no_pr_and_says_why`, `::test_a_promoted_scope_opens_its_pr_ready_for_review` |
| Revalidation demoting a pack, or touching one not resolved on a waiver or unverified | `test_evidence::test_revalidate_never_demotes`, `::test_revalidate_leaves_a_proven_resolution_alone`, `::test_revalidate_does_nothing_with_the_bar_off` |
| Two agents holding the same pack, or a pack writer overwriting another's change | `test_pack_write_integrity::test_concurrent_claims_have_exactly_one_winner`, `::test_a_second_claim_gets_409_unless_forced`. Writers that load, check and write a pack hold `pack_lock` for the whole step |
| Record a class guard by a sha prefix (a mistyped one waits as `pending` forever, like an unsynced mirror) | `test_class_packs::test_record_guard_validates` |
| A class guard closing a child its rule never fired on, a noqa-waived hit counting as gone, or a silence-only closure counting toward `class_packs.lifecycle` | `test_class_packs::test_child_closes_only_when_its_rule_hit_is_gone`, `::test_a_waived_hit_is_not_a_closure`, `::test_rule_hits_ignores_noqa_and_never_reads_a_failure_as_clean`, `::test_lifecycle_gate_counts_rule_attributed_closures_only` |
| Write a finding class onto a pack without all three models agreeing, or with an unmeasured prompt | `test_pack_classes::test_consensus_needs_every_model`, `::test_no_frozen_prompt_skips` |
| A gate passing below its minimum sample | `test_trust_eval::test_failing_and_insufficient`, `::test_stale_or_missing_seeded_eval_is_insufficient`, `test_guard_seeded::test_min_sample_scales_with_proven_guards`, `::test_stale_or_missing_results_are_insufficient` |
| Count a guard seed as caught or missed when the guard's check already fails on the unplanted export, or count an unproven guard toward `guards.seeded_recall` | `test_guard_seeded::test_a_check_that_already_fails_unplanted_is_invalid_not_a_miss`, `::test_no_proven_guards_is_insufficient`, `::test_a_rule_that_cannot_run_makes_the_seed_invalid` |
| Count a guard seed's variant (`dodges`, `fixes`) toward `guards.seeded_recall`, or a dodge of a seed the guard missed | `test_guard_seeded::test_a_missed_dodge_leaves_seeded_recall_alone`, `::test_a_dodge_of_a_seed_the_guard_misses_is_not_counted` |
| The structure scan reading an unreadable repo or file as clean, writing a mirror, or losing a cycle to a lazy import, re-export, relay or package `__init__` | `test_structure::test_an_unreadable_repo_or_file_is_never_clean`, `::test_run_reads_origin_dev_and_never_writes_the_mirror`, `test_structure_python::test_an_unusable_interpreter_is_an_error_never_a_fallback`, `::test_the_image_parses_every_python_it_can_test`, `::test_a_cycle_stays_one_however_it_is_dressed`, `::test_a_relay_module_joins_the_cycle`, `::test_a_cycle_through_a_package_init_is_found` |
| A new module cycle in this repo (imports inside functions count; the known list may only shrink) | `test_own_structure::test_no_new_module_cycle_in_this_repo` |
| Resolve a structural pack whose violation is still there, moved, or unread, or on a waiver alone | `test_structural_evidence::test_a_dodge_is_not_gone`, `::test_unknown_is_never_gone`, `::test_no_shortcut_or_waiver_resolves_a_cycle_that_is_still_there`, `::test_a_patchs_tests_cannot_rewrite_the_tree_that_is_judged` |
| Overwrite `trust_history.jsonl`, `trust_eval_history.jsonl`, `pack_savings_history.jsonl` or `autonomy_history.jsonl` | `test_trust_metrics::test_refresh_writes_report_and_appends_history`, `test_trust_eval::test_evaluate_writes_report_and_history`, `test_pack_savings::test_refresh_writes_report_and_appends_history`, `test_autonomy::test_refresh_moves_each_scope_one_step_and_appends_history` |
| A partial RAG send retiring other docs | `test_rag_retire::test_sync_retires_only_what_the_send_replaced`, `::test_failed_delete_is_retried_and_failed_send_retires_nothing` |
| Remove a GitHub label this tool didn't add, or label an external item | `test_linchpin::test_apply_labels_adds_removes_only_its_own_and_records_ledger` |
| Keep calling a local LLM backend after it failed 5 times in a row (each unit paying its timeout), or retry a 4xx | `test_llm_client::test_dead_backend_trips_the_breaker_for_every_client`, `::test_only_transient_errors_are_retried`. `AI_AUDIT_LLM_BREAKER_FAILURES` / `_COOLDOWN_S` (5, 600s) |
| Take a GPU lease for a cloud LLM provider, let a broker that's down or answers 4xx block or fail a run, count a refused or timed-out lease as an LLM-backend failure, or call a broker-supplied node URL that is a cloud metadata address | `test_gpu_lease::test_a_cloud_provider_never_leases`, `::test_unreachable_broker_fails_open_to_the_configured_endpoint`, `::test_broker_4xx_fails_open_and_is_logged`, `::test_a_refused_lease_skips_the_model_without_tripping_the_breaker`, `::test_unsafe_node_urls_are_refused_and_the_lease_released` |
| A wiki build crashing, and publishing nothing, when Anthropic refuses it for the workspace usage limit (it stops like the lane budget) | `test_wiki_usage_limit::test_a_usage_limited_stage_stops_the_build_instead_of_crashing`, `::test_a_usage_limited_sync_request_stops_the_stage` |
| Spend past a lane budget, or a conductor call that isn't recorded (cloud models; local ones are free. **UNENFORCED** for OpenAI and xAI in the scan path, `llm_client.py`) | `test_spend_ledger::test_lane_and_total_caps`, `::test_batch_prefetch_trims_to_budget`; `test_llm_batch4::test_a_spent_lane_refuses_every_cloud_model`, `::test_a_spent_lane_skips_cloud_fallbacks_and_runs_a_local_one`, `::test_every_litellm_call_is_recorded` |
| Suite-rule findings that disagree with argus-ci's CI (a different catalogue or checker) | `test_suite_rules::test_catalogue_is_read_from_dev_not_the_checkout`; the scanner runs argus-ci's own `check.py` |
| A patch's tests, installs or checks seeing the host env (API keys, tokens, `DATABASE_URL`, live service URLs such as the RAG ingest URL) | `auditor/proc.py` `child_env` allowlist at every child that runs audited code; `test_validate_child_env` |
| A container without an init process (git's detached gc leaves zombies that exhaust its pids) | `test_compose_init::test_every_service_runs_under_init` |
| A scanned repo missing from the ecosystem.md table or flow diagram, `service_roles`, or (argus repos) the brand stages | `test_repo_sets::test_every_scanned_repo_has_an_ecosystem_table_row`, `::test_every_scanned_repo_is_in_the_flow_diagram`, `::test_every_scanned_repo_has_a_service_role`, `::test_every_scanned_argus_repo_has_a_brand_stage` (`NOT_ARGUS` lists the scanned non-argus repos, which have no stage) |
| A flow-diagram arrow into argus-retina that `allowed_calls` doesn't list, or an allowed retina caller the diagram leaves out (a `a / b ──► c` group links every member) | `test_repo_sets::test_the_flow_draws_exactly_the_allowed_calls_into_retina` |
| A home directory checked in, or `~` in config read as the container's `HOME` (`/`) | `test_host_path::test_no_home_directory_is_checked_in`, `::test_host_home_wins_over_the_containers`, `::test_no_usable_home_is_an_error_not_a_root_path`, `::test_compose_passes_the_hosts_home_to_every_service` |
| Serve a brand SVG with scripts or event handlers | `test_ui_topology::test_mark_rejects_active_content` |
| An agent-facing request whose cost grows with the number of packs, or seconds of CPU-bound work inside the API process | `test_hot_paths::test_agent_hot_paths_cost_the_same_at_any_pack_count`, `::test_topology_rebuilds_outside_the_api_process`. At runtime, requests over `AI_AUDIT_SLOW_REQUEST_MS` (1000) are counted in `/audit/health` `slow_requests`, which should stay empty |
| README commands that differ from `cli.py`, or a scanned repo's name, internal host, private address or home path in the README (text; check screenshots by eye) | `test_readme::test_readme_commands_match_the_cli`, `::test_readme_names_no_scanned_repo_or_internal_host`, `::test_no_private_addresses_or_home_paths`; rebuild the table with `scripts/update_readme_cli.py` |
| A non-GET endpoint without `_require_mutating_auth`, or a request value spliced into git as an option | `test_api_auth_sweep::test_every_non_get_route_requires_the_token` (exemptions listed with a reason in `OWN_AUTH`), `::test_refs_that_could_become_git_options_are_refused` |
| A status webhook sent without the bearer token its target names, or one failing target stopping the others or the wrap-up | `test_status_webhooks::test_a_target_whose_token_is_unset_is_skipped_not_sent_bare`, `::test_a_failing_target_never_stops_the_others`, `::test_bad_config_is_reported_and_never_fatal` |
| Work pushed to `main`, a PR into `main` other than the release, or `main` ahead of `dev` except the release merge commit | `scripts/check_branch_rules.py` in the `Branch rules` workflow: `pr-base` fails a PR into `main` whose head isn't this repo's `dev`; `main-tree` fails when `main`'s tree differs from its merge base with `dev` (`test_branch_rules`). A direct push to `main` is caught after the fact, not refused: that needs branch protection |
| Deploy hooks in dragonhound-site, or docs committed into it | **UNENFORCED** here (another repo). Docs go live only by a `docs.lock.json` pin bump on its `dev` |

## Setup and commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]            # ai-audit CLI; .[semantic] adds faiss-cpu; .[rules] the pinned semgrep
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

**Mirrors.** The scan set is every `smk762/argus-*` except `argus-docs` (generated output), plus `agent-composer`,
`voluptas` and this repo (from its own mirror, never the working checkout), under `~/<name>` or
`~/mirrors/<name>` (`config/repo_mirrors.yaml`). `ai-audit rest-run` syncs them to `dev` first, and
`scripts/sync_repo_mirrors.sh` does it by hand. A mirror is fast-forwarded, or reset with a backup branch after an
upstream rewrite, and a mirror with a unique local commit is never touched (`conductor/mirrors.py`). Mirrors only sync nightly, so `validate_patch` fetches `dev` itself (`auditor/validate_base.py`); the container has no git credential helper, so private repos (argus-iris, voluptas) fetch with `GITHUB_TOKEN`, offered only to `https://github.com`. SSHFS mounts
are deprecated. `../argus-core` is a symlink alias for the cortex mirror: suite repos import `@../argus-core/SUITE.md`
on purpose. Topology: [`ecosystem/ecosystem.md`](ecosystem/ecosystem.md).

**Auth.** Read-only pages and JSON need no auth. Every action goes through `_require_mutating_auth`: on this host,
`AI_AUDIT_API_TOKEN` as `X-API-Token`. The same value is in agent-composer's `.env` and in the `env` block of
`~/.claude/settings.json`, so coding agents in any repo have it.

**Findings and runs.** Only repo audits store findings rows; ecosystem runs store none, so they never count as scans
(trust metrics, GC, the findings trend). A nightly run records the files it couldn't review per repo (`extra.unreviewed_files`), and a `Scan` doesn't `cover` them, so a budget-capped night is a blind spot rather than a night of fixes. Findings match on repo, file, type and title, never line, and LLM rewordings in
the same file are paired by title word overlap (`auditor/finding_match.py`). Pack dedupe uses the same match.

**The scan's LLM prompt** is `AUDIT_INSTRUCTIONS_V2` in `auditor/llm_client.py`, chosen by the 2026-11-13 eval.
`AI_AUDIT_LLM_PROMPT=v3` is opt-in until measured. `repo_review` in `config/prompts.yaml` is **not** what the scan uses.

**validate_patch** (`auditor/validator.py`, `validate_patch_tree`) applies the patch to a clean `git archive` of the
`dev` tip, never the live checkout. That tree gets its own `.git` with `HEAD` at the base and the base's tags (objects borrowed through alternates), so tests that read git history run as in CI. Each changed file maps to its nearest project: for `.py`, a dir with
`pyproject.toml`, `setup.py`, `requirements.txt` or `tests/`; for TS/JS, the nearest `package.json`
(`auditor/validate_node.py`). A `.py` file with no project marker above it (argus-ci) runs the `test_*.py` beside it from the repo top, one pytest run per directory, in a venv of what the repo's CI installs by name (`--with`, `pip install`) plus a fixed import map (#102). That project's tests or npm checks then run in a cached env built from its declared deps, on the image's Python unless the project's `requires-python` rules it out; then on the oldest version that satisfies it, which uv fetches into the venv cache (`validator.test_python`, #141).
Failures are compared against the unpatched tree, so only failures the patch introduced count; one of those that passes on a single rerun is reported as a flake, and tests that error on both trees are reported apart from ones that fail on both. When nothing can run,
the result is `lint_only` (Python) or skipped (TS/JS), with a note saying why. A hatch-vcs or setuptools-scm version
module gets a stub. The response names its base (`base_source`, `base_stale`, `behind_by`). A request's `base_ref` (#91) pins an older base for a fix that has already merged; it must be an ancestor of the `dev` tip, and both the patched and the "before" runs use it. One test module that imports an undeclared package stops pytest's collection, so every patch to that project validates lint-only: `ai-audit test-env` (nightly, `auditor/test_env_readiness.py`) collects each project's tests in those same envs and reports the missing modules (`/audit/test-env.json`, Repos page). It must run on the validator's interpreter (the Dockerfile's `ARG PYTHON_VERSION`): from the host it runs itself in the API image, refuses if it can't, and a report from another interpreter is served with `stale_interpreter` (#136).

**Evidence bar** (`auditor/evidence.py`, #58). A pass resolves a pack only with evidence, else it becomes
`unverified`, which is still open work. Bug, security and untyped finding packs need a test from the patch that fails
on `dev` and passes patched. The "fails on `dev`" run copies every test-side file the patch changes (test files, and
anything under a `tests/` dir) onto the dev tree whole, so a helper *defined inside* a test file runs its patched
version there too: a bug in such a helper can't show repro. That is deliberate (mixing dev helpers into a patched test
file would let a test-helper change manufacture the failure the code change is meant to earn), so a patch that only
touches test files resolves as `test_only`, and a mixed patch whose proof needs the old helper takes a waiver.
Other code changes need tests or npm checks to have run. Docs/config-only patches pass.
The evidence records what the tests skipped (`tests`, `validator.pytest_counts`; no counts means unknown, not zero), and a run where nothing passed leaves the pack `unverified`. A `repro` can't rest on a skipped test; `tests`, `test_only` and `structure` can.
`evidence_waiver` (≥ 20 chars) resolves anyway and is recorded. Fix commits carry `Pack:` / `Evidence:` trailers
(`auditor/trailers.py`), which the trust metrics read from `git log`.

**Trust** (epic #64). `auditor/trust_metrics.py` measures whether agent fixes can be trusted, and every trust-upgrade
change is judged against it. `auditor/trust_eval.py` defines the gates: below its minimum sample a gate is
`insufficient`, never a pass. The exit criteria are every gate that isn't pending or informational (`INFORMATIONAL`: today
`class_packs.lifecycle`, `guards.dodge_recall` and `guards.fix_silence`, so guards are judged by `guards.seeded_recall`) passing in each of the last two ISO weeks,
with headline metrics not worse over 4 weekly snapshots. `auditor/eval_seeded.py` tests the evidence bar against
planted bugs in real mirrors, and `auditor/guard_seeded.py` plants each guarded class's seeds (`config/guard_seeds.yaml`)
in a scratch export to measure every proven class guard's recall (`guards.seeded_recall`); both refresh weekly inside
the gates step. Never tune a seed to make a guard pass: a miss is a finding about the guard. The same goes for a seed's variants (#155). These run nightly after the repo audit, in this order: pack GC, pack classes, class
packs, test-env readiness, the structure scan, revalidation, trust metrics, gates. The evidence gates judge the last 14 days of resolutions
(`GATE_WINDOW_DAYS`).

**Autonomy** (#61, `auditor/autonomy.py`, `config/autonomy.yaml`). The conductor asks `autonomy.decide` before each
PR: `report` (no PR), `draft_pr` (the default and the floor), `ready_pr` (the ceiling; `auto_merge` stays off until the
config raises it). Each (repo, finding class) scope moves at most one level a night, after the trust metrics, on
`trust.json`'s `by_scope`: its last 20 mature agent PRs. A breach of any threshold demotes it, never below the floor;
promotion needs the full window clean, every pack resolved with evidence (no lint-only, no waiver), and 30 days at its
level. Auth, secrets, migrations and data deletion never go above `draft_pr`. `reports/autonomy_history.jsonl` is
append-only: it is the record a promotion rests on.

**Finding classes** (#53, `auditor/finding_class.py`, `config/finding_classes.yaml`). The gold set quotes real
findings, so it lives outside the repo in `~/.config/ai-code-auditor/eval/` (`AI_AUDIT_EVAL_DIR`), and tests point that
at an empty dir. Only the blind-labelled `test` set counts toward the gates (precision ≥ 95%, coverage ≥ 80%). As of
2026-11-16 on that set: title rules 87.5% / 32%, the three models agreeing 95.3% / 86%. The council relabelling the gold
set is made of the models being graded, so read scores as agreement, not ground truth. A class is written onto a pack
only when all three models agree, the class has a guard, and the frozen prompt in `reports/_classes/` exists
(`auditor/pack_classes.py`). One class-guard pack per guardable class closes a child only when its proven guard's
own rule (`--guard --rule ruff:B905,…`) fired on the child's code just before the guard, by shared identifiers, never
line numbers, and no longer fires on `dev`, noqa ignored. The scan's silence alone closes nothing. A guard with no
static rule closes no children, and semantic fixes go through `validate_patch` (`auditor/class_packs.py`).

**Suite rules** (#55, `auditor/suite_rules.py`). The lint catalogue lives in argus-ci (`suite-rules/`), not here. The
scanner runs argus-ci's own `check.py` on the catalogue exported from the mirror's `origin/dev`, so its findings match
CI. Guard-rule hits become findings (source `suite`) whose class comes from `config/finding_classes.yaml` with no model
call. Convention rules such as `silent-except` are not findings. The `rules.fixtures` gate needs at least 5 rules, all
passing their fixtures. In CI, a repo's `.suite-rules-baseline.json` may only shrink.

**Structure** (#154, `auditor/structure.py`, `ai-audit structure`) finds module cycles and private imports inside each
repo, from the mirror's `origin/dev`, Python only. It parses in a child on the image's `AI_AUDIT_SCAN_PYTHON` (newer than the image's own), so newer syntax isn't unreadable; `auditor/structure.py` stays stdlib-only for that. The scan only reports (`reports/structure.json`); its suggested cuts are a hint, wrong about half the time when hand-checked.
A cycle gets a pack only once approved on `/ui/architecture` with a note on what to move (`auditor/structure_packs.py`; decisions in `rules/structure_decisions.json`, outside git). An agent can fill that note in advance (`POST /audit/cycles/{id}/proposal`, `auditor/structure_proposals.py`), and the decision records whether it was approved as written or edited.
`validate_patch` can already judge one (`structure.judge`, #58): it scans fresh exports of dev and dev plus the patch
in a child process, and compares members, not keys. `agent_pack.STRUCTURE_KINDS` and `evidence.STRUCTURE_KINDS` must match. Every import inside a function counts, because that is how
all the cycles it finds today avoid an import error.

**Pack GC** (`auditor/pack_gc.py`) and revalidation are the only things besides an agent or a person that close finding
packs. GC closes `open` packs as `obsolete`, which means "no longer reported", not a fix. A re-found issue reopens
the same pack. A `wontfix` pack is what keeps a re-found false positive closed, so GC closes one only when its file or
cited lines are gone, or it duplicates a pack now resolved or obsolete; never for not being rescanned. Architecture packs (the ecosystem run's policy violations) keep one id per issue across runs (`architecture_pack_id`: rule plus violating path without its line), and that run closes the open ones it no longer reports, in repos it scanned and read whole (`ecosystem/unread.py`; `pack_gc.close_unreported_architecture`, `reports/pack_gc_architecture.json`). Revalidation (`auditor/revalidate.py`, `ai-audit revalidate`) re-sends a merged fix for a waived or
unverified pack through `validate_patch` with `revalidate=true` and only ever records stronger evidence.

**Night page.** Mission outcomes come from the mission files, because mithrandir's worker stats don't count `PARTIAL`.
Spend comes from the ledger's `conductor` lane, because `cost_usd_est` is not filled in.

**Ollama healthcheck** hits `/api/tags` (no model load), not generate.

**GPU leases** (#51, `auditor/gpu_lease.py`). gothmog and the nvidia-smi/imogen/vidita polling are gone: with a
local-Ollama provider, each repo's model calls in the nightly pass hold one argus-iris `ollama:<model>` lease (`batch`,
taken at the first uncached call), and any other Ollama call takes its own. Off unless `ARGUS_GPU_COORDINATOR=broker`
(cortex's `ARGUS_GPU_*` variables; tenant `ai-code-auditor`, manifest `gpu-tenant.yaml`). The calls go to the grant's
vetted `base_url`. A broker that's down or answers 4xx fails open to `AI_AUDIT_LLM_ENDPOINT`; a refusal or timeout
makes no model calls for that repo, so its files are unreviewed (a blind scan). Each lease is in `extra.gpu_leases`.
Footprints are `config/gpu_lease.yaml`'s geometry; `qwen2.5-coder:32b` (about 22 GB) fits neither 8 GB Ollama node.

**Web UI.** Static pages in `auditor/static/` share `ui.css` and `ui.js`, and put `<!--#nav-->` where the server
injects the nav. Pages have no `<style>` or `style=""`: their rules sit at the end of `ui.css` under
`:where(.page-<name>)` on `<body>`, which keeps the cascade a page-local block had (`test_ui::test_every_page_uses_shared_shell`).
There is no build step, and each page has its own JSON endpoint. Markdown goes through
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
`python scripts/emit_agent_packs.py` rebuilds world-model and architecture packs, syncs extraction packs, and leaves
finding packs and their status alone. Extraction candidates are proposals: only one approved on `/ui/architecture`
(a decision in `rules/extraction_decisions.json`, outside git) has a pack, and that pack is several PRs, so
`validate_patch` moves it to `in_progress` and the nightly sync resolves it once every copy is gone from `dev`
(`auditor/extraction.py`). Candidate ids follow their units across runs, not their rank. Each candidate carries a `copy_risk` (non-test copies,
churn, agent PRs touching the copies), which orders "To decide". In a bundle, a consolidation pack (an approved
extraction, or a `workaround` finding from `auditor/workaround.py`) goes ahead of fixes in the same directory (#60). `bash scripts/install_agent_pack_awareness.sh` writes the flow into every ecosystem repo on disk
(local files only, never git). Re-run it after changing the API base or the flow text.

## Branches

`dev` is the default branch and the base for every PR. Cut feature branches from `origin/dev` and bring them up to date by merging `origin/dev`, never `main`. Never push work to `main` or open a PR against it, except the release PR from `dev`: `main` must never be ahead of `dev`, with one exception. A release fast-forwards `main` to a `dev` commit that passed CI (`git push origin origin/dev:main`), then tags it. If `main` is protected, the release is a reviewed PR from `dev` into `main`, and merging it leaves a merge commit on `main` alone. That merge commit is the one exception to the rule: it leaves `main` one commit ahead of `dev` with an identical tree. Leave it there and don't merge `main` back into `dev`. Anything else on `main` that `dev` lacks is a mistake: `git diff $(git merge-base origin/dev origin/main) origin/main` must be empty (right after a release that is `git diff origin/dev origin/main`; CI checks it). Once `main` carries such a commit it can't be fast-forwarded, so later releases are PRs too.
