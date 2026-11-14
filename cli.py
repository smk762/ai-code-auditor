from __future__ import annotations

import argparse
import json
from dataclasses import asdict
import os
from pathlib import Path
import yaml

from auditor.auth import enforce_role
from auditor.config import load_ecosystem_config
from auditor.runtime import require_auth
from copilot.copilot_engine import CopilotEngine
from ecosystem.graph_builder import GraphBuilder
from pipelines.ecosystem_audit import run as run_ecosystem_audit
from pipelines.nightly_repo_audit import run as run_repo_audit
from patterns.pattern_feedback import append_architecture_rule_for_extraction
from qa.benchmark import run_benchmark
from semantic.embedding_index import EmbeddingIndex
from semantic.semantic_search import SemanticSearch


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Code Auditor CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    repo = sub.add_parser("repo-run", help="Run nightly repo audit pipeline")
    repo.add_argument("--auth-code", default="", help="Auth code when auth mode is enabled")
    repo.add_argument("--auth-token", default="", help="JWT token with admin role")
    repo.add_argument(
        "--batch",
        action="store_true",
        help="Review uncached units via the Anthropic Batches API first (half price, slower)",
    )
    repo.add_argument("--model", default="", help="Override AI_AUDIT_LLM_MODEL for this run (e.g. claude-opus-5-5)")
    repo.add_argument("--effort", default="", help="Override AI_AUDIT_LLM_EFFORT (low|medium|high|xhigh|max)")
    repo.add_argument(
        "--estimate",
        action="store_true",
        help="With --batch: print how many units would be reviewed and the estimated cost, then exit",
    )

    budget = sub.add_parser("budget", help="Show this budget month's Claude API spend by lane and model")
    budget.add_argument("--json", action="store_true", help="Machine-readable output")
    budget.add_argument("--record", type=float, default=None, metavar="USD",
                        help="Record spend made outside the auditor (e.g. evals) against --lane")
    budget.add_argument("--lane", default="eval", help="Lane for --record (default: eval)")
    budget.add_argument("--note", default="", help="Note for --record")

    eco = sub.add_parser("ecosystem-run", help="Run ecosystem audit pipeline")
    eco.add_argument("--auth-code", default="", help="Auth code when auth mode is enabled")
    eco.add_argument("--auth-token", default="", help="JWT token with admin role")

    rest = sub.add_parser(
        "rest-run",
        help="Night conductor rest session (scanners → Opus workers → draft PRs → mithrandir LATEST)",
    )
    rest.add_argument("--dry-run", action="store_true", help="Stub workers; still publish LATEST")
    rest.add_argument("--force", action="store_true", help="Run outside the rest window")
    rest.add_argument("--no-webhook", action="store_true", help="Skip Jarrah companion webhook")
    rest.add_argument("--no-rag", action="store_true", help="Skip slim RAG ingest")
    rest.add_argument("--no-draft-prs", action="store_true")
    rest.add_argument("--no-pr-review", action="store_true")
    rest.add_argument("--auth-code", default="")
    rest.add_argument("--auth-token", default="")

    ask = sub.add_parser("ask", help="Ask ecosystem copilot a question")
    ask.add_argument("query", type=str, help="Natural language query")
    ask.add_argument("--json", action="store_true", help="Output as JSON")
    ask.add_argument("--deep", action="store_true", help="Run iterative deep research mode")
    ask.add_argument("--iterations", type=int, default=3, help="Deep research iterations")
    ask.add_argument(
        "--context-profile",
        choices=("lean", "balanced", "deep"),
        default="balanced",
        help="Context budget profile for ask mode",
    )
    ask.add_argument(
        "--max-context-tokens",
        type=int,
        default=2200,
        help="Maximum tokens to keep in curated context window",
    )
    ask.add_argument("--disable-compaction", action="store_true", help="Disable context compaction step")
    ask.add_argument("--disable-notes", action="store_true", help="Disable persistent context note-taking")
    ask.add_argument("--enable-subagents", action="store_true", help="Enable specialist sub-agent summaries")
    ask.add_argument(
        "--agent-brief",
        action="store_true",
        help="Append an external/paid-agent briefing section to the answer",
    )
    ask.add_argument("--notes-path", default="memory/context_notes.md", help="Path to persistent context notes")
    ask.add_argument("--auth-code", default="", help="Auth code when auth mode is enabled")
    ask.add_argument("--auth-token", default="", help="JWT token with analyst role")

    srch = sub.add_parser("search", help="Semantic search over the audit RAG collection (wiki sections, packs) via rag-chat")
    srch.add_argument("query", type=str, help="Natural language query")
    srch.add_argument("--repo", default="", help="Only hits tagged repo:<name>")
    srch.add_argument("--kind", default="", help="Only hits tagged kind:<kind> (wiki, agent_coding_pack, ...)")
    srch.add_argument("--top-k", type=int, default=5, help="Number of hits (1-20)")
    srch.add_argument("--json", action="store_true", help="Print rag-chat's JSON response")

    qa = sub.add_parser("qa-benchmark", help="Run benchmark dataset for precision/recall")
    qa.add_argument("--dataset", default="qa/fixtures/benchmark_queries.yaml", help="Path to benchmark dataset")
    qa.add_argument("--json", action="store_true", help="Output as JSON")
    qa.add_argument("--auth-token", default="", help="JWT token with analyst role")

    approve = sub.add_parser("approve-extraction", help="Approve extraction candidate and auto-generate architecture rule")
    approve.add_argument("--candidate-id", required=True, help="Candidate id from reports/extraction_candidates.yaml")
    approve.add_argument("--report", default="reports/extraction_candidates.yaml", help="Extraction candidate YAML report path")
    approve.add_argument("--rules-path", default="config/architecture_rules.yaml", help="Architecture rules file to append")
    approve.add_argument("--auth-token", default="", help="JWT token with admin role")

    repair_cmd = sub.add_parser("repair", help="Run agentic repair loop via rag-chat repair API")
    repair_cmd.add_argument(
        "--repo", default="",
        help="Repo name from ecosystem config (optional with --pack-id: taken from the pack)",
    )
    repair_cmd.add_argument(
        "--pack-id", dest="repair_pack_id", default="", metavar="ID",
        help="Drive the repair from an agent pack: rag-chat fetches GET /audit/packs/ID (+ brief) "
             "and injects acceptance criteria, tests and constraints. A diff source is then optional.",
    )
    repair_cmd.add_argument(
        "--audit-api-base", dest="repair_audit_api_base", default="", metavar="URL",
        help="Audit API base rag-chat uses to fetch --pack-id "
             "(default: rag-chat's AI_AUDIT_PUBLIC_BASE_URL / AUDIT_API_URL)",
    )
    _repair_src = repair_cmd.add_mutually_exclusive_group(required=False)
    _repair_src.add_argument("--diff", dest="repair_diff_file", metavar="FILE", help="Path to a unified diff file")
    _repair_src.add_argument("--stdin", dest="repair_stdin", action="store_true", help="Read unified diff from stdin")
    _repair_src.add_argument("--compare-branch", dest="repair_compare_branch", metavar="BRANCH", help="Compare HEAD against BRANCH")
    repair_cmd.add_argument("--mode", choices=("auto_fix", "review", "suggest"), default="review")
    repair_cmd.add_argument("--fix-model", default="", help="Override fixer model")
    repair_cmd.add_argument("--critique-model", default="", help="Enable critic with this model")
    repair_cmd.add_argument("--adversarial-model", default="", help="Enable adversary with this model")
    repair_cmd.add_argument("--max-iterations", type=int, default=3)
    repair_cmd.add_argument("--apply", action="store_true", help="Apply final patch in-place")
    repair_cmd.add_argument(
        "--validate-tests", dest="validate_tests", action="store_true", default=True,
        help="Run repo test/lint suite after each patch (default: on)",
    )
    repair_cmd.add_argument(
        "--no-validate-tests", dest="validate_tests", action="store_false",
        help="Skip test-suite validation (faster, static-audit only)",
    )
    # Git automation flags (only meaningful with --apply --mode auto_fix)
    repair_cmd.add_argument("--commit", action="store_true", help="Create a repair branch and commit the patch after apply")
    repair_cmd.add_argument("--push", action="store_true", help="Push the repair branch after commit (implies --commit)")
    repair_cmd.add_argument("--base-branch", default="", help="Base branch for the repair branch (default: repo default)")
    repair_cmd.add_argument("--remote", default="origin", help="Remote name for push (default: origin)")
    repair_cmd.add_argument("--author-name", default="ai-code-auditor", help="Git commit author name")
    repair_cmd.add_argument("--author-email", default="noreply@ai-audit.local", help="Git commit author e-mail")
    repair_cmd.add_argument("--repair-api-url", default="", help="rag-chat base URL (default: RAG_CHAT_URL env, else the rag-ingest host on :9150)")
    repair_cmd.add_argument("--auth-code", default="")
    repair_cmd.add_argument("--auth-token", default="", help="ai-audit JWT, checked locally only (never sent to rag-chat)")
    repair_cmd.add_argument(
        "--rag-chat-api-key",
        default="",
        help="rag-chat API key, required for --apply/--commit/--push (default: RAG_CHAT_API_KEY env; "
        "prefer the env var so the key stays out of shell history and ps)",
    )

    diff_cmd = sub.add_parser("diff-audit", help="Audit only files touched by a diff")
    diff_cmd.add_argument("--repo", required=True, help="Repo name from ecosystem config")
    _diff_src = diff_cmd.add_mutually_exclusive_group(required=True)
    _diff_src.add_argument("--diff", dest="diff_file", metavar="FILE", help="Path to a unified diff file")
    _diff_src.add_argument("--stdin", action="store_true", help="Read unified diff from stdin")
    _diff_src.add_argument("--compare-branch", metavar="BRANCH", help="Compare HEAD against BRANCH (git diff BRANCH...HEAD)")
    diff_cmd.add_argument("--json", action="store_true", help="Output findings as JSON")
    diff_cmd.add_argument("--auth-code", default="", help="Auth code when auth mode is enabled")
    diff_cmd.add_argument("--auth-token", default="", help="JWT token with analyst role")

    packs = sub.add_parser(
        "packs",
        help="List/search agent packs (world model + specialist coding packs)",
    )
    packs.add_argument("--repo", default="", help="Filter by repo name substring")
    packs.add_argument("--severity", default="", help="Filter by severity (CRITICAL/HIGH/...)")
    packs.add_argument(
        "--kind",
        default="",
        help="Filter by kind (world_model/architecture/extraction/security/...)",
    )
    packs.add_argument(
        "--layer",
        default="",
        help="Filter by layer: world_model | specialist",
    )
    packs.add_argument("--q", default="", help="Substring match on id/title/repo/path")
    packs.add_argument("--run-id", default="", help="Filter by conductor/diff run id")
    packs.add_argument("--limit", type=int, default=50)
    packs.add_argument("--id", dest="pack_id", default="", help="Fetch one pack by id")
    packs.add_argument("--brief", action="store_true", help="Print markdown brief (with --id)")
    packs.add_argument("--json", action="store_true", help="JSON output")
    packs.add_argument(
        "--status",
        default="",
        help="Filter by status: open|claimed|in_progress|resolved|wontfix (comma-separated)",
    )
    packs.add_argument(
        "--bundle",
        action="store_true",
        help="Mission bundle for --repo NAME: world model + ranked specialists, token-capped",
    )
    packs.add_argument("--token-budget", type=int, default=2500, help="Bundle brief cap (~4 chars/token)")
    packs.add_argument("--limit-specialists", type=int, default=5, help="Max specialists in bundle")
    packs.add_argument(
        "--set-status",
        default="",
        choices=["", "open", "claimed", "in_progress", "resolved", "wontfix"],
        help="Set lifecycle status on --id PACK_ID",
    )
    packs.add_argument("--note", default="", help="Optional note with --set-status")

    merge = sub.add_parser(
        "merge-refresh",
        help="Lightweight merge-to-dev refresh: sync mirror → diff packs → RAG (debounced via API, or --now)",
    )
    merge.add_argument("--repo", required=True, help="Repo name from ecosystem / mirrors config")
    merge.add_argument("--before", default="", help="Pre-merge SHA (optional)")
    merge.add_argument("--after", default="", help="Post-merge SHA (optional; default HEAD after sync)")
    merge.add_argument(
        "--now",
        action="store_true",
        help="Run immediately in-process (skip debounce scheduler)",
    )
    merge.add_argument(
        "--schedule",
        action="store_true",
        help="POST /audit/merge on the local API (honours debounce + global gap)",
    )
    merge.add_argument("--api-base", default="", help="Audit API base (default AI_AUDIT_PUBLIC_BASE_URL)")
    merge.add_argument("--no-sync", action="store_true", help="Skip mirror fetch (local path only)")
    merge.add_argument("--no-rag", action="store_true", help="Skip pack RAG ingest")
    merge.add_argument("--json", action="store_true")

    poll = sub.add_parser(
        "merge-poll",
        help="Poll GitHub API for dev tip changes across mirrors; enqueue merge-refresh for moved SHAs",
    )
    poll.add_argument(
        "--schedule",
        action="store_true",
        help="POST each change to audit-api /audit/merge (recommended under systemd)",
    )
    poll.add_argument(
        "--local",
        action="store_true",
        help="Enqueue into this process's scheduler (default when audit-api is not targeted)",
    )
    poll.add_argument(
        "--baseline-only",
        action="store_true",
        help="Record current tips without enqueueing refreshes (first-run priming)",
    )
    poll.add_argument("--api-base", default="", help="Audit API base for --schedule")
    poll.add_argument("--json", action="store_true")
    poll.add_argument(
        "--no-pr-intel",
        action="store_true",
        help="Skip the blocked-PR intel refresh that follows each poll",
    )

    pri = sub.add_parser(
        "pr-intel",
        help="Blocked-PR intel across mirrors: blockers, resolution path, spoken brief (read-only)",
    )
    pri.add_argument("--json", action="store_true", help="Print the full report as JSON")
    pri.add_argument("--cached", action="store_true", help="Show the last report instead of querying GitHub")

    sav = sub.add_parser(
        "pack-savings",
        help="Estimated token savings from agent packs vs crawling, per repo (tracked over time)",
    )
    sav.add_argument("--json", action="store_true", help="Print the full report as JSON")
    sav.add_argument("--cached", action="store_true", help="Show the last report instead of re-measuring")
    sav.add_argument("--history", action="store_true", help="Show the recorded trend instead")

    wk = sub.add_parser("wiki", help="Generated per-repo wiki (DeepWiki-style): build or show status")
    wk_sub = wk.add_subparsers(dest="wiki_command", required=True)
    wkb = wk_sub.add_parser("build", help="Build wiki pages (notes → drafts → check-and-fix) via the Batch API")
    wkb.add_argument("--repos", default="", help="Comma-separated repo names (default: every enabled repo)")
    wkb.add_argument("--dry-run", action="store_true", help="Price the build without calling the API")
    wkb.add_argument("--sync", action="store_true", help="Streaming Messages API instead of Batch (2x price, no wait)")
    wkb.add_argument("--json", action="store_true", help="Print the build report as JSON")
    wkb.add_argument("--public", action="store_true",
                     help="Build the public variant (public argus repos only, scrubbed inputs) under reports/wiki/_public/")
    wke = wk_sub.add_parser("export", help="Gated public bundle for dragonhound.dev (public argus repos only; no API spend)")
    wke.add_argument("--out", default="", help="Bundle directory (default: <output_dir>/wiki_public)")
    wke.add_argument("--denylist", default="", help="Private deny-list file (default: $AI_AUDIT_OPSEC_DENYLIST or "
                     "~/.config/ai-code-auditor/opsec-denylist.txt); the export won't run without one")
    wke.add_argument("--json", action="store_true", help="Print the export report as JSON")
    wkp = wk_sub.add_parser("publish", help="Commit the exported bundle to the private docs repo (bot identity)")
    wkp.add_argument("--bundle", default="", help="Bundle directory (default: <output_dir>/wiki_public)")
    wkp.add_argument("--repo", default="smk762/argus-docs", help="Docs repo (owner/name or a git URL)")
    wkp.add_argument("--checkout", default="", help="Local checkout (default: $AI_AUDIT_DOCS_CHECKOUT or "
                     "~/.cache/ai-code-auditor/argus-docs); machine-managed, local edits are discarded")
    wkp.add_argument("--push", action="store_true", help="Push the commit (without it: commit locally only)")
    wkp.add_argument("--denylist", default="", help="Private deny-list file (as for export)")
    wkp.add_argument("--json", action="store_true", help="Print the result as JSON")
    wkr = wk_sub.add_parser("refresh", help="Rebuild stale pages (weekly at most per repo), then export + publish")
    wkr.add_argument("--repos", default="", help="Comma-separated repo names (default: every enabled repo)")
    wkr.add_argument("--force", action="store_true", help="Rebuild every page now, ignoring staleness and the weekly limit")
    wkr.add_argument("--dry-run", action="store_true", help="Show the plan and its price; no API calls, nothing written")
    wkr.add_argument("--no-publish", action="store_true", help="Skip export + publish to the docs repo")
    wkr.add_argument("--no-push", action="store_true", help="Commit to the docs checkout but don't push")
    wkr.add_argument("--all-public", action="store_true",
                     help="Also build a public variant for public repos whose internal pages already pass the gate")
    wkr.add_argument("--json", action="store_true", help="Print the run record as JSON")
    wki = wk_sub.add_parser("ingest", help="Send wiki sections to the audit RAG collection (rag-ingest)")
    wki.add_argument("--repos", default="", help="Comma-separated repo names (default: every built repo)")
    wki.add_argument("--json", action="store_true", help="Print the result as JSON")
    wks = wk_sub.add_parser("status", help="Show built repos, their source SHAs and the wiki lane budget")
    wks.add_argument("--json", action="store_true", help="Print as JSON")

    return parser


def _run_search(args) -> None:
    import json as _json

    from auditor import rag_client

    try:
        res = rag_client.search(args.query, repo=args.repo or None, kind=args.kind or None, top_k=args.top_k)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"search failed: {exc}") from exc
    if args.json:
        print(_json.dumps(res, indent=2))
        return
    hits = res.get("hits") or []
    tags = ", ".join(res.get("tags") or []) or "no filter"
    print(f"{len(hits)} hit(s) from {res.get('collection')} ({tags})")
    for i, h in enumerate(hits, 1):
        print(f"\n{i}. {h.get('title') or h.get('doc_id')}  [{h.get('score')}]")
        if h.get("url"):
            print(f"   {h['url']}")
        text = " ".join((h.get("text") or "").split())
        print(f"   {text[:300]}{'…' if len(text) > 300 else ''}")


def _run_wiki(args) -> None:
    import json as _json

    from auditor.config import load_ecosystem_config
    from auditor.spend_ledger import get_ledger

    out = load_ecosystem_config().output_dir
    if args.wiki_command == "ingest":
        from wiki.rag import ingest

        names = [n.strip() for n in args.repos.split(",") if n.strip()]
        res = ingest(out, {n: None for n in names} if names else None)
        if args.json:
            print(_json.dumps(res, indent=2))
            return
        if "skipped" in res:
            print(f"skipped: {res['skipped']}")
            return
        for repo, r in res["repos"].items():
            print(f"  {repo:22} {'ok' if r.get('ingested') else 'FAILED'} {r.get('count', 0)} sections"
                  + (f" ({r.get('status_code', '')} {r.get('error', '')})" if not r.get("ingested") else ""))
        if res.get("error"):
            print(res["error"])
        print(f"{res['count']} sections ingested")
        return
    if args.wiki_command == "refresh":
        from wiki.refresh import refresh

        names = [n.strip() for n in args.repos.split(",") if n.strip()] or None
        rec = refresh(out, names, force=args.force, dry_run=args.dry_run,
                      publish=not args.no_publish, push=not args.no_push, all_public=args.all_public)
        if args.json:
            print(_json.dumps(rec, indent=2))
            return
        for item in rec["plan"]:
            print(f"  rebuild   {item['repo'] + '/' + item['variant']:32} {', '.join(item['pages'])}")
        for key, why in sorted(rec["skipped"].items()):
            print(f"  skip      {key:32} {why}")
        for item in rec.get("deferred") or []:
            print(f"  deferred  {item['repo'] + '/' + item['variant']:32} ~${item['estimate_usd']:.2f} (over the wiki budget left)")
        money = rec["estimate_usd"] if args.dry_run else rec["cost_usd"]
        print(f"{'estimated' if args.dry_run else 'cost'} ${money:.2f}")
        for variant, b in rec["builds"].items():
            if b["stopped"]:
                print(f"{variant} build stopped: {b['stopped']}")
        if pub := rec.get("publish"):
            print("publish: " + (pub["error"] if "error" in pub else
                  f"{'new commit ' + str(pub['pin']['commit'])[:10] if pub['changed'] else 'unchanged'}"
                  + (" (pushed)" if pub.get("pushed") else "")))
        return
    if args.wiki_command == "publish":
        from pathlib import Path as _P

        from wiki.publish import publish

        res = publish(args.bundle or _P(out) / "wiki_public", repo=args.repo, checkout=args.checkout or None,
                      push=args.push, denylist_path=args.denylist or None)
        if args.json:
            print(_json.dumps(res, indent=2))
            return
        if not res["changed"]:
            print(f"unchanged: {res['repo']} already has bundle {res['bundle_sha256'][:12]} (commit {str(res['commit'])[:10]})")
            return
        print(f"committed {str(res['commit'])[:10]} to {res['checkout']} ({len(res['repos'])} repos)"
              + (" and pushed" if res["pushed"] else "; not pushed (add --push)"))
        print("docs.lock.json for dragonhound-site:")
        print(_json.dumps(res["pin"], indent=2))
        return
    if args.wiki_command == "export":
        from wiki.public import export

        rep = export(out, args.out or None, denylist_path=args.denylist or None).to_dict()
        if args.json:
            print(_json.dumps(rep, indent=2))
            return
        print(f"bundle: {rep['out_dir']}")
        for name, pages in rep["exported"].items():
            print(f"  exported  {name:22} {', '.join(pages)}")
        for name, why in rep["skipped"].items():
            print(f"  skipped   {name:22} {why}")
        if rep["refused"]:
            print(f"{len(rep['refused'])} page(s) refused by the opsec gate; details (with the matched text) in "
                  f"{out}/wiki_public_report.json, which stays outside the bundle")
        return
    if args.wiki_command == "status":
        from wiki.build import wiki_dir

        try:
            idx = _json.loads((wiki_dir(out) / "INDEX.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            idx = {"repos": {}}
        led = get_ledger()
        payload = {**idx, "wiki_spent_usd": round(led.spent("wiki"), 2), "wiki_cap_usd": led.caps().get("wiki"),
                   "wiki_left_usd": round(led.remaining("wiki"), 2)}
        if args.json:
            print(_json.dumps(payload, indent=2))
            return
        print(f"wiki lane: ${payload['wiki_spent_usd']:.2f} spent of ${payload['wiki_cap_usd'] or 0:.2f} "
              f"this budget month (${payload['wiki_left_usd']:.2f} left)")
        for name, r in sorted(idx.get("repos", {}).items()):
            print(f"  {name:22} {str(r.get('source_sha') or '')[:10]:10} {r.get('pages', 0)} pages  built {r.get('built_at')}")
        if not idx.get("repos"):
            print("  no repos built yet: ai-audit wiki build --repos <name> --dry-run")
        return

    from wiki.build import build

    names = [n.strip() for n in args.repos.split(",") if n.strip()] or None
    rep = build(names, out, mode="sync" if args.sync else "batch", dry_run=args.dry_run,
                public=getattr(args, "public", False)).to_dict()
    if args.json:
        print(_json.dumps(rep, indent=2))
        return
    if rep["dry_run"]:
        print(f"dry run: {len(rep['repos'])} repos, {rep['stages']['notes']['requested']} files need notes, "
              f"estimated ${rep['estimate_usd']:.2f} ({'sync' if args.sync else 'batch'}); "
              f"wiki lane has ${rep['wiki_lane_left_usd']:.2f} left")
        for name, r in rep["repos"].items():
            if r["skipped"]:
                print(f"  {name}: skipped ({r['skipped']})")
        return
    for name, r in rep["repos"].items():
        state = r["skipped"] or f"{len(r['pages_written'])} pages written, {r['dropped_claims']} claims dropped"
        failed = f"; failed: {r['pages_failed']}" if r["pages_failed"] else ""
        print(f"  {name:22} {state}{failed}")
    print(f"cost ${rep['cost_usd']:.2f}; wiki lane has ${rep['wiki_lane_left_usd']:.2f} left")
    if rep["stopped"]:
        raise SystemExit(f"stopped: {rep['stopped']}")


def _refresh_pr_intel_quietly() -> dict:
    """merge-poll tail: refresh blocked-PR intel; never fails the poll."""
    from auditor.pr_intel import pr_intel_enabled, refresh

    if not pr_intel_enabled():
        return {"skipped": "AI_AUDIT_PR_INTEL=0"}
    try:
        return {"counts": refresh()["counts"]}
    except Exception as exc:  # noqa: BLE001 - the poll result matters more
        return {"error": str(exc)[:300]}


def _pct(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:>5.0%}"


def _run_pack_savings(*, as_json: bool, cached: bool, history: bool) -> None:
    from auditor.pack_savings import load_history, load_report, refresh

    if history:
        rows = load_history()
        if as_json:
            print(json.dumps(rows, indent=2))
            return
        if not rows:
            raise SystemExit("No pack savings history yet; run `ai-audit pack-savings` first.")
        print(f"{'measured':16} {'packs':>6} {'bundle':>7} {'session':>8} {'median':>7} {'file/brief':>10}")
        for r in rows:
            ratio = r.get("median_file_to_brief")
            print(
                f"{r['generated_at'][:16]:16} {r.get('pack_count') or 0:>6} {r['bundle_tokens']:>7} "
                f"{_pct(r.get('session_saving')):>8} {_pct(r.get('median_saving_vs_targeted')):>7} "
                f"{(f'{ratio:.1f}x' if ratio else 'n/a'):>10}"
            )
        return
    report = load_report() if cached else refresh()
    if report is None:
        raise SystemExit("No cached pack savings; run `ai-audit pack-savings` first.")
    if as_json:
        print(json.dumps(report, indent=2))
        return
    print(f"{'repo':20} {'source':>9} {'orient':>7} {'bundle':>7} {'in/cand':>8} {'targeted':>9} {'vs tgt':>7} {'vs src':>7}")
    for r in report["repos"]:
        print(
            f"{r['repo']:20} {r['source_tokens']:>9} {r['orientation_tokens']:>7} {r['bundle_tokens']:>7} "
            f"{r['specialists_in_bundle']:>3}/{r['specialist_candidates']:<4} {r['targeted_tokens']:>9} "
            f"{_pct(r['saving_vs_targeted']):>7} {_pct(r['saving_vs_source']):>7}"
        )
    s, p = report["summary"], report["per_pack"]
    print(
        f"\nmedian saving vs targeted crawl {_pct(s['median_saving_vs_targeted']).strip()} · "
        f"session (incl. orientation) {_pct(s['session_saving']).strip()} · "
        f"vs source crawl {_pct(s['saving_vs_source']).strip()}"
    )
    if p.get("packs"):
        print(
            f"per pack (n={p['packs']}): brief {p['median_brief_tokens']} vs cited file "
            f"{p['median_cited_file_tokens']} tokens, {p['median_file_to_brief']}x "
            f"(p25 {p['p25_file_to_brief']}x, p75 {p['p75_file_to_brief']}x)"
        )
    if s["repos_with_broken_imports"]:
        print("CLAUDE.md imports that don't resolve: " + ", ".join(
            f"{r['repo']} ({', '.join(r['broken_imports'])})" for r in report["repos"] if r["broken_imports"]
        ))
    print(f"({report['method']} estimate, {report['generated_at'][:16]}Z)")


def _run_pr_intel(*, as_json: bool, cached: bool) -> None:
    from auditor.pr_intel import load_report, refresh

    if cached:
        report = load_report()
        if report is None:
            raise SystemExit("No cached PR intel; run `ai-audit pr-intel` first.")
    else:
        try:
            report = refresh()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    if as_json:
        print(json.dumps(report, indent=2))
        return
    c = report["counts"]
    print(
        f"{c['open']} open · {c['blocked']} blocked · {c['waiting']} waiting · "
        f"{c['ready']} ready · {c['draft']} draft   ({report['generated_at'][:16]}Z)"
    )
    for p in report["prs"]:
        if p["state"] in ("blocked", "waiting", "pending"):
            kinds = ", ".join(b["kind"] for b in p["blockers"] if b["kind"] != "review_required")
            print(f"  {p['state']:8s} {p['ref']:26s} [{kinds}] → {p['next_action']}")
    print("\nResolution path:")
    merges: list[str] = []
    for s in report["plan"]:
        plain = s["action"] == "Merge" and not (s["release"] or s["unblocks"] or s["after"])
        if plain:
            merges.append(s["ref"])
            continue
        if merges:
            print(f"  · merge {len(merges)} ready: {', '.join(merges)}")
            merges = []
        note = f"  (unblocks {', '.join(s['unblocks'])})" if s["unblocks"] else ""
        if s["release"]:
            note = "  (release)" + note
        print(f"  {s['step']:>2}. {s['ref']}: {s['action']}{note}")
    if merges:
        print(f"  · merge {len(merges)} ready: {', '.join(merges)}")
    if report.get("spoken"):
        print(f"\nBriefing: {report['spoken']}")
    for e in report.get("errors") or []:
        print(f"  ! {e['repo']}: {e['error']}")


def _run_ask(
    query: str,
    as_json: bool = False,
    deep: bool = False,
    iterations: int = 3,
    context_profile: str = "balanced",
    max_context_tokens: int = 2200,
    disable_compaction: bool = False,
    disable_notes: bool = False,
    enable_subagents: bool = False,
    agent_brief: bool = False,
    notes_path: str = "memory/context_notes.md",
    auth_code: str = "",
    auth_token: str = "",
) -> None:
    cfg = load_ecosystem_config()
    provided_auth = auth_code or os.getenv("AI_AUDIT_AUTH_CODE_INPUT", "")
    require_auth(cfg.auth_mode, cfg.auth_code_env, provided_code=provided_auth)
    token = auth_token or os.getenv("AI_AUDIT_AUTH_TOKEN", "")
    if token:
        enforce_role(token, "analyst", "cli:ask")
    index = EmbeddingIndex(cfg.embedding_index_path)
    search = SemanticSearch(index)
    graph = GraphBuilder(cfg.graph_db_path)
    try:
        engine = CopilotEngine(search=search, graph=graph)
        if deep:
            deep_result = engine.deep_research(query, max_iterations=max(iterations, 1))
            if as_json:
                print(json.dumps(deep_result, indent=2))
            else:
                print("Research Plan:")
                print(deep_result["plan"])
                print("\nUpdates:")
                for step in deep_result["updates"]:
                    print(f"- Iteration {step['iteration']}: {step['insight']}")
                print("\nConclusion:")
                print(deep_result["conclusion"])
                if deep_result["citations"]:
                    print("\nCitations:")
                    for cite in deep_result["citations"][:10]:
                        print(f"- {cite}")
            return
        response = engine.ask(
            query,
            context_profile=context_profile,
            max_context_tokens=max_context_tokens,
            enable_compaction=not disable_compaction,
            enable_notes=not disable_notes,
            enable_subagents=enable_subagents,
            external_agent_brief=agent_brief,
            notes_path=notes_path,
        )
    finally:
        graph.close()

    if as_json:
        print(json.dumps(asdict(response), indent=2))
        return

    print(response.answer)
    if response.citations:
        print("\nCitations:")
        for cite in response.citations:
            print(f"- {cite}")


def _run_repair(
    repo_name: str,
    repair_diff_file: str = "",
    repair_stdin: bool = False,
    repair_compare_branch: str = "",
    mode: str = "review",
    fix_model: str = "",
    critique_model: str = "",
    adversarial_model: str = "",
    max_iterations: int = 3,
    apply: bool = False,
    validate_tests: bool = True,
    commit: bool = False,
    push: bool = False,
    base_branch: str = "",
    remote: str = "origin",
    author_name: str = "ai-code-auditor",
    author_email: str = "noreply@ai-audit.local",
    repair_api_url: str = "",
    auth_code: str = "",
    auth_token: str = "",
    pack_id: str = "",
    audit_api_base: str = "",
    rag_chat_api_key: str = "",
) -> None:
    """Forward a repair request to the rag-chat repair API and stream SSE output.

    Two separate credentials:
    - the ai-audit JWT (``--auth-token`` / ``AI_AUDIT_AUTH_TOKEN``) is checked locally with
      ``enforce_role`` and never leaves this process;
    - the rag-chat API key (``--rag-chat-api-key`` / ``RAG_CHAT_API_KEY``) is sent to rag-chat as
      ``Authorization: Bearer``. rag-chat requires it for write runs (``apply`` or ``git_ops``);
      read-only review/suggest runs work without it.
    """
    import sys
    import requests as _req

    cfg = load_ecosystem_config()
    provided_auth = auth_code or os.getenv("AI_AUDIT_AUTH_CODE_INPUT", "")
    require_auth(cfg.auth_mode, cfg.auth_code_env, provided_code=provided_auth)

    if not (repair_diff_file or repair_stdin or repair_compare_branch or pack_id):
        raise SystemExit("Provide one of --diff, --stdin, --compare-branch, or --pack-id.")
    if not (repo_name or pack_id):
        raise SystemExit("Provide --repo (or --pack-id to take the repo from the pack).")

    # --commit / --push only make sense when --apply is also set; without it the
    # repair API has no patch to commit and would either no-op or commit empty.
    if (commit or push) and not apply:
        raise SystemExit("--commit / --push require --apply (no patch would be written).")
    if push and not commit:
        commit = True  # documented behavior: --push implies --commit

    # Repair can apply patches, create branches, commit, and push — gate it the
    # same way as the other write-capable commands.  ``analyst`` is sufficient
    # for read-only review/suggest; write paths require ``admin``.
    write_ops = bool(apply or commit or push)
    token = auth_token or os.getenv("AI_AUDIT_AUTH_TOKEN", "")
    if token:
        enforce_role(token, "admin" if write_ops else "analyst", "cli:repair")

    from auditor.rag_client import chat_url

    base_url = repair_api_url or chat_url()
    rag_key = (rag_chat_api_key or os.getenv("RAG_CHAT_API_KEY", "")).strip()
    if write_ops and not rag_key:
        raise SystemExit(
            "--apply / --commit / --push need a rag-chat API key (rag-chat rejects unauthenticated "
            "write runs with 401). Set RAG_CHAT_API_KEY in .env or pass --rag-chat-api-key. "
            f"Create a key at {base_url}/ui/api-keys (or POST {base_url}/api-keys). "
            "Read-only runs (--mode review / suggest without --apply) need no key."
        )

    # Acquire diff text
    if repair_compare_branch:
        diff_text = ""  # the repair API will generate this from compare_branch
    elif repair_diff_file:
        diff_text = Path(repair_diff_file).read_text(encoding="utf-8")
    elif repair_stdin:
        diff_text = sys.stdin.read()
    else:
        diff_text = ""  # pack-only: rag-chat works from the pack's context

    git_ops = None
    if commit or push:
        git_ops = {
            "enabled": True,
            "commit": True,
            "push": push,
            "base_branch": base_branch,
            "remote": remote,
            "author_name": author_name,
            "author_email": author_email,
        }

    payload = {
        "repo": repo_name,
        "diff": diff_text,
        "compare_branch": repair_compare_branch,
        "mode": mode,
        "fix_model": fix_model,
        "critique_model": critique_model,
        "adversarial_model": adversarial_model,
        "max_iterations": max_iterations,
        "apply": apply,
        "validate_tests": validate_tests,
        **({"git_ops": git_ops} if git_ops else {}),
        **({"pack_id": pack_id} if pack_id else {}),
        **({"audit_api_base": audit_api_base} if pack_id and audit_api_base else {}),
    }

    # Only the rag-chat key goes over the wire; the ai-audit JWT is not a rag-chat credential.
    headers = {"Authorization": f"Bearer {rag_key}"} if rag_key else {}
    try:
        with _req.post(
            f"{base_url}/api/repair/run",
            json=payload,
            headers=headers,
            stream=True,
            timeout=600,
        ) as resp:
            if resp.status_code in (401, 403):
                detail = ""
                try:
                    detail = str(resp.json().get("detail", ""))
                except ValueError:
                    pass
                hint = (
                    "check that RAG_CHAT_API_KEY / --rag-chat-api-key is a valid rag-chat API key"
                    if rag_key else
                    "this run needs a rag-chat API key: set RAG_CHAT_API_KEY or pass --rag-chat-api-key"
                )
                raise SystemExit(
                    f"rag-chat refused the repair run ({resp.status_code}"
                    f"{': ' + detail if detail else ''}); {hint}."
                )
            resp.raise_for_status()
            event_name = ""
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    print(f"\n[{event_name}]")
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                        if event_name == "pack_loaded":
                            print(f"  Pack:          {data.get('pack_id')} — {data.get('title')}")
                            print(f"  Repo:          {data.get('repo')}")
                        elif event_name == "warning":
                            print(f"  Warning: {data.get('message')}")
                        elif "fixer_response" in data:
                            it = data.get("iteration", "?")
                            print(f"  Iteration {it} — patch extracted: {bool(data.get('patch'))}")
                            v = data.get("validation_passed")
                            if v is not None:
                                print(f"  Static audit:  {'PASS' if v else 'FAIL'}")
                                if not v and data.get("validation_errors"):
                                    for e in data["validation_errors"][:5]:
                                        print(f"    {e}")
                            tv = data.get("test_validation_passed")
                            if tv is not None:
                                print(f"  Test suite:    {'PASS' if tv else 'FAIL'}")
                                if not tv and data.get("test_validation_errors"):
                                    for e in data["test_validation_errors"][:10]:
                                        print(f"    {e}")
                        elif "final_patch" in data:
                            print(f"  Applied:       {data.get('applied')}")
                            if data.get("branch"):
                                print(f"  Branch:        {data['branch']}")
                            if data.get("commit_sha"):
                                print(f"  Commit:        {data['commit_sha'][:12]}")
                            if data.get("push_url"):
                                print(f"  Pushed to:     {data['push_url']}")
                            if data.get("git_error"):
                                print(f"  Git warning:   {data['git_error']}")
                            if data.get("final_patch"):
                                print("\n--- Final patch ---")
                                print(data["final_patch"][:4000])
                        elif "message" in data:
                            print(f"  Error: {data['message']}")
                    except json.JSONDecodeError:
                        print(line)
    except _req.exceptions.ConnectionError:
        raise SystemExit(f"Could not connect to repair API at {base_url}. Is rag-chat running?")


def _run_diff_audit(
    repo_name: str,
    diff_file: str = "",
    use_stdin: bool = False,
    compare_branch: str = "",
    as_json: bool = False,
    auth_code: str = "",
    auth_token: str = "",
) -> None:
    import sys

    from auditor.diff_auditor import get_diff_from_branch, run_diff_audit
    from auditor.repo_resolver import resolve_repo_path

    cfg = load_ecosystem_config()
    provided_auth = auth_code or os.getenv("AI_AUDIT_AUTH_CODE_INPUT", "")
    require_auth(cfg.auth_mode, cfg.auth_code_env, provided_code=provided_auth)
    token = auth_token or os.getenv("AI_AUDIT_AUTH_TOKEN", "")
    if token:
        enforce_role(token, "analyst", "cli:diff-audit")

    repo = next((r for r in cfg.repos if r.name == repo_name), None)
    if repo is None:
        raise SystemExit(f"Repo {repo_name!r} not found in ecosystem config.")

    if compare_branch:
        repo_path = resolve_repo_path(repo)
        diff_text = get_diff_from_branch(repo_path, compare_branch)
    elif diff_file:
        diff_text = Path(diff_file).read_text(encoding="utf-8")
    else:
        diff_text = sys.stdin.read()

    if not diff_text.strip():
        print("No diff content — nothing to audit.")
        return

    result = run_diff_audit(repo, diff_text)

    if as_json:
        print(json.dumps({
            "repo": result.repo,
            "files_changed": len(result.impact.directly_changed),
            "files_affected": len(result.impact.transitively_affected),
            "units_analyzed": len(result.units_analyzed),
            "findings": [asdict(f) for f in result.findings],
            "generated_at": result.generated_at,
        }, indent=2))
        return

    print(f"Repo:                    {result.repo}")
    print(f"Files changed:           {len(result.impact.directly_changed)}")
    print(f"Files transitively hit:  {len(result.impact.transitively_affected)}")
    print(f"Units analyzed:          {len(result.units_analyzed)}")
    print(f"Findings:                {len(result.findings)}")
    if result.findings:
        print()
        for f in result.findings:
            print(f"  [{f.severity}] {f.file_path}:{f.line}  {f.title}")
            if f.description:
                print(f"    {f.description[:120]}")


def _post_merge_refresh(api_base: str, payload: dict) -> dict:
    """POST a merge-refresh request to the audit API ``/audit/merge`` endpoint.

    Raises ``urllib.error.HTTPError`` / ``URLError`` on failure.
    """
    import urllib.request

    base = (
        api_base
        or os.getenv("AI_AUDIT_PUBLIC_BASE_URL")
        or "http://127.0.0.1:8765"
    ).rstrip("/")
    headers = {"Content-Type": "application/json"}
    tok = os.getenv("AI_AUDIT_API_TOKEN", "").strip()
    if tok:
        headers["X-API-Token"] = tok
    req = urllib.request.Request(
        f"{base}/audit/merge",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _budget(args: argparse.Namespace) -> None:
    from auditor.spend_ledger import get_ledger

    ledger = get_ledger()
    if args.record is not None:
        ledger.record_manual(args.lane, args.record, args.note)
    summary = ledger.summary()
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    cap = summary["total_cap"]
    print(f"Budget month from {summary['month_start']}: ${summary['total_spent']:.2f} spent"
          + (f" of ${cap:.2f} (${cap - summary['total_spent']:.2f} left)" if cap is not None else ""))
    for lane, info in summary["lanes"].items():
        lane_cap = f"${info['cap']:.2f}" if info["cap"] is not None else "no cap"
        print(f"  {lane:12s} ${info['spent']:8.2f}  cap {lane_cap:>8s}  left ${info['remaining']:.2f}")
    if summary["by_model"]:
        print("By lane/model:")
        for r in summary["by_model"]:
            print(f"  {r['lane']:12s} {r['model']:20s} {r['calls']:6d} calls  "
                  f"{r['input_tokens'] or 0:>11,} in  {r['output_tokens'] or 0:>10,} out  ${r['cost']:.2f}")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    cfg = load_ecosystem_config()
    provided_auth = getattr(args, "auth_code", "") or os.getenv("AI_AUDIT_AUTH_CODE_INPUT", "")
    require_auth(cfg.auth_mode, cfg.auth_code_env, provided_code=provided_auth)
    token = getattr(args, "auth_token", "") or os.getenv("AI_AUDIT_AUTH_TOKEN", "")
    if token and args.command in {"repo-run", "ecosystem-run", "rest-run"}:
        enforce_role(token, "admin", f"cli:{args.command}")
    if args.command == "budget":
        _budget(args)
        return
    if args.command == "repo-run":
        if args.model:
            os.environ["AI_AUDIT_LLM_MODEL"] = args.model
        if args.effort:
            os.environ["AI_AUDIT_LLM_EFFORT"] = args.effort
        if args.batch:
            os.environ["AI_AUDIT_LLM_BATCH"] = "1"
            os.environ["AI_AUDIT_SPEND_LANE"] = "audit_batch"
        if args.estimate:
            from auditor.batch_review import prefetch_reviews

            stats = prefetch_reviews(cfg.repos, dry_run=True)
            print(json.dumps(stats.__dict__, indent=2, default=str))
            return
        run_repo_audit()
    elif args.command == "ecosystem-run":
        run_ecosystem_audit()
    elif args.command == "rest-run":
        from pipelines.rest_session import run as run_rest

        result = run_rest(
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            no_webhook=bool(args.no_webhook),
            no_rag=bool(args.no_rag),
            no_draft_prs=bool(args.no_draft_prs),
            no_pr_review=bool(args.no_pr_review),
        )
        print(json.dumps({
            "run_id": (result.get("status") or {}).get("run_id"),
            "event_id": (result.get("status") or {}).get("event_id"),
            "severity": (result.get("status") or {}).get("severity"),
            "requires_ack": (result.get("status") or {}).get("requires_ack"),
            "latest_url": (result.get("status") or {}).get("latest_url"),
            "webhook": result.get("webhook"),
            "agent_packs": result.get("agent_packs"),
        }, indent=2))
    elif args.command == "packs":
        from auditor.agent_pack import list_packs, load_pack, rebuild_index
        from conductor.publish import public_base_url

        cfg = load_ecosystem_config()
        out = cfg.output_dir
        if args.bundle:
            from auditor.agent_pack import build_pack_bundle

            if not args.repo:
                raise SystemExit("--bundle requires --repo NAME (exact repo name)")
            bundle = build_pack_bundle(
                out,
                repo=args.repo,
                token_budget=args.token_budget,
                q=args.q,
                limit_specialists=args.limit_specialists,
                status=args.status or "open,claimed,in_progress",
                public_base_url=public_base_url(),
            )
            if args.json:
                print(json.dumps(bundle, indent=2))
            else:
                print(bundle["briefs_markdown"])
                print(
                    f"\n--- bundle repo={bundle['repo']} specialists={len(bundle['specialists'])} "
                    f"~{bundle['approx_tokens']}/{bundle['token_budget']} tokens "
                    f"truncated={bundle['truncated']} — GET {bundle['discovery']['bundle']}"
                )
        elif args.set_status:
            from auditor.agent_pack import refresh_pack_rag, update_pack_status

            if not args.pack_id:
                raise SystemExit("--set-status requires --id PACK_ID")
            updated = update_pack_status(
                out,
                args.pack_id,
                args.set_status,
                args.note,
                public_base_url=public_base_url(),
            )
            if updated is None:
                raise SystemExit(f"Pack not found: {args.pack_id}")
            refresh_pack_rag(out, updated)
            print(json.dumps({"id": args.pack_id, "status": updated["status"]}, indent=2))
        elif args.pack_id:
            data = load_pack(out, args.pack_id)
            if data is None:
                raise SystemExit(f"Pack not found: {args.pack_id}")
            if args.brief:
                from auditor.agent_pack import AgentCodingPack

                pack = AgentCodingPack.from_dict(data, fallback_id=args.pack_id)
                print(pack.to_brief_md())
            elif args.json:
                print(json.dumps(data, indent=2))
            else:
                print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        else:
            rebuild_index(out, public_base_url=public_base_url())
            entries = list_packs(
                out,
                repo=args.repo,
                severity=args.severity,
                kind=args.kind,
                layer=getattr(args, "layer", "") or "",
                q=args.q,
                run_id=args.run_id,
                status=args.status,
                limit=args.limit,
            )
            payload = {
                "count": len(entries),
                "packs": entries,
                "list_url": f"{public_base_url()}/audit/packs",
                "bundle_url": f"{public_base_url()}/audit/packs/bundle?repo=<name>",
                "world_model_url": f"{public_base_url()}/audit/packs?kind=world_model",
            }
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                for e in entries:
                    print(
                        f"{e.get('layer', '?'):11} {e.get('kind', '?'):14} "
                        f"{e.get('severity', '?'):8}  {e.get('repo', ''):20}  "
                        f"{e.get('id', '')}  {e.get('title', '')[:50]}"
                    )
                print(f"\n{len(entries)} packs — GET {payload['list_url']}")
                print(f"World model — GET {payload['world_model_url']}")
    elif args.command == "ask":
        _run_ask(
            args.query,
            as_json=args.json,
            deep=args.deep,
            iterations=args.iterations,
            context_profile=args.context_profile,
            max_context_tokens=args.max_context_tokens,
            disable_compaction=args.disable_compaction,
            disable_notes=args.disable_notes,
            enable_subagents=args.enable_subagents,
            agent_brief=args.agent_brief,
            notes_path=args.notes_path,
            auth_code=args.auth_code,
            auth_token=args.auth_token,
        )
    elif args.command == "search":
        _run_search(args)
    elif args.command == "qa-benchmark":
        if token:
            enforce_role(token, "analyst", "cli:qa-benchmark")
        result = run_benchmark(args.dataset)
        if args.json:
            print(json.dumps(asdict(result), indent=2))
        else:
            print(f"Precision: {result.precision:.3f}")
            print(f"Recall: {result.recall:.3f}")
            print(f"Total queries: {result.total_queries}")
    elif args.command == "repair":
        _run_repair(
            repo_name=args.repo,
            repair_diff_file=args.repair_diff_file or "",
            repair_stdin=args.repair_stdin,
            repair_compare_branch=args.repair_compare_branch or "",
            mode=args.mode,
            fix_model=args.fix_model,
            critique_model=args.critique_model,
            adversarial_model=args.adversarial_model,
            max_iterations=args.max_iterations,
            apply=args.apply,
            validate_tests=args.validate_tests,
            commit=args.commit,
            push=args.push,
            base_branch=args.base_branch,
            remote=args.remote,
            author_name=args.author_name,
            author_email=args.author_email,
            repair_api_url=args.repair_api_url,
            auth_code=args.auth_code,
            auth_token=args.auth_token,
            pack_id=args.repair_pack_id,
            audit_api_base=args.repair_audit_api_base,
            rag_chat_api_key=args.rag_chat_api_key,
        )
    elif args.command == "merge-refresh":
        from auditor.merge_hook import MergeEvent, get_scheduler, run_merge_refresh

        if args.schedule and args.now:
            raise SystemExit("Use only one of --schedule or --now")
        if args.schedule:
            import urllib.error

            try:
                body = _post_merge_refresh(
                    args.api_base,
                    {
                        "repo": args.repo,
                        "before": args.before,
                        "after": args.after,
                        "force": False,
                        "sync_mirror": not args.no_sync,
                    },
                )
            except urllib.error.HTTPError as exc:
                raise SystemExit(f"schedule failed: {exc.code} {exc.read().decode('utf-8', 'replace')}") from exc
            except urllib.error.URLError as exc:
                raise SystemExit(f"schedule failed: audit API unreachable ({exc.reason})") from exc
            print(json.dumps(body) if args.json else json.dumps(body, indent=2))
        elif args.now:
            result = run_merge_refresh(
                MergeEvent(
                    repo=args.repo,
                    before=args.before,
                    after=args.after,
                    source="cli",
                    force=True,
                ),
                sync_mirror=not args.no_sync,
                ingest_rag=False if args.no_rag else None,
            )
            print(json.dumps(result, indent=2))
        else:
            # Default: enqueue into this process's debounced scheduler and wait for
            # it to drain — its timer/worker are daemon threads that die on exit.
            sched = get_scheduler()
            queued = sched.enqueue(
                MergeEvent(
                    repo=args.repo,
                    before=args.before,
                    after=args.after,
                    source="cli",
                    force=False,
                    sync_mirror=not args.no_sync,
                )
            )
            print(json.dumps(queued, indent=2))
            if queued.get("accepted"):
                sched.wait_idle()
                print(json.dumps(sched.status().get("recent") or [], indent=2))
    elif args.command == "merge-poll":
        from auditor.merge_hook import get_scheduler
        from auditor.merge_poll import poll_dev_tips

        if args.schedule and args.local:
            raise SystemExit("Use only one of --schedule or --local")
        # --schedule: detect changes here, POST each into audit-api worker.
        # --local / default: enqueue into this process and wait for it to drain.
        dispatch = None
        if args.schedule:
            def dispatch(event):  # noqa: E306 - closure over args.api_base
                return _post_merge_refresh(
                    args.api_base,
                    {
                        "repo": event.repo,
                        "before": event.before,
                        "after": event.after,
                        "force": True,
                        "sync_mirror": True,
                    },
                )

        result = poll_dev_tips(
            enqueue=not args.baseline_only,
            baseline_only=bool(args.baseline_only),
            force_queue=True,
            dispatch=dispatch,
        )
        if not args.schedule and result.queued:
            get_scheduler().wait_idle()
        out = result.to_dict()
        if not args.no_pr_intel:
            out["pr_intel"] = _refresh_pr_intel_quietly()
        print(json.dumps(out, indent=2))
    elif args.command == "pr-intel":
        _run_pr_intel(as_json=args.json, cached=args.cached)
    elif args.command == "pack-savings":
        _run_pack_savings(as_json=args.json, cached=args.cached, history=args.history)
    elif args.command == "wiki":
        _run_wiki(args)
    elif args.command == "diff-audit":
        _run_diff_audit(
            repo_name=args.repo,
            diff_file=args.diff_file or "",
            use_stdin=args.stdin,
            compare_branch=args.compare_branch or "",
            as_json=args.json,
            auth_code=args.auth_code,
            auth_token=args.auth_token,
        )
    elif args.command == "approve-extraction":
        if token:
            enforce_role(token, "admin", "cli:approve-extraction")
        report_path = Path(args.report)
        if not report_path.exists():
            raise FileNotFoundError(f"Extraction report not found: {report_path}")
        payload = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        candidates = payload.get("candidates", [])
        target = next((c for c in candidates if c.get("id") == args.candidate_id), None)
        if target is None:
            raise ValueError(f"Candidate id not found: {args.candidate_id}")
        append_architecture_rule_for_extraction(target, rules_path=args.rules_path)
        print(f"Approved candidate {args.candidate_id} and updated {args.rules_path}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
