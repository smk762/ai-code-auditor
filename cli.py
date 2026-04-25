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

    eco = sub.add_parser("ecosystem-run", help="Run ecosystem audit pipeline")
    eco.add_argument("--auth-code", default="", help="Auth code when auth mode is enabled")
    eco.add_argument("--auth-token", default="", help="JWT token with admin role")

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

    qa = sub.add_parser("qa-benchmark", help="Run benchmark dataset for precision/recall")
    qa.add_argument("--dataset", default="qa/fixtures/benchmark_queries.yaml", help="Path to benchmark dataset")
    qa.add_argument("--json", action="store_true", help="Output as JSON")
    qa.add_argument("--auth-token", default="", help="JWT token with analyst role")

    approve = sub.add_parser("approve-extraction", help="Approve extraction candidate and auto-generate architecture rule")
    approve.add_argument("--candidate-id", required=True, help="Candidate id from reports/extraction_candidates.yaml")
    approve.add_argument("--report", default="reports/extraction_candidates.yaml", help="Extraction candidate YAML report path")
    approve.add_argument("--rules-path", default="config/architecture_rules.yaml", help="Architecture rules file to append")
    approve.add_argument("--auth-token", default="", help="JWT token with admin role")

    diff_cmd = sub.add_parser("diff-audit", help="Audit only files touched by a diff")
    diff_cmd.add_argument("--repo", required=True, help="Repo name from ecosystem config")
    _diff_src = diff_cmd.add_mutually_exclusive_group(required=True)
    _diff_src.add_argument("--diff", dest="diff_file", metavar="FILE", help="Path to a unified diff file")
    _diff_src.add_argument("--stdin", action="store_true", help="Read unified diff from stdin")
    _diff_src.add_argument("--compare-branch", metavar="BRANCH", help="Compare HEAD against BRANCH (git diff BRANCH...HEAD)")
    diff_cmd.add_argument("--json", action="store_true", help="Output findings as JSON")
    diff_cmd.add_argument("--auth-code", default="", help="Auth code when auth mode is enabled")
    diff_cmd.add_argument("--auth-token", default="", help="JWT token with analyst role")

    return parser


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


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    cfg = load_ecosystem_config()
    provided_auth = getattr(args, "auth_code", "") or os.getenv("AI_AUDIT_AUTH_CODE_INPUT", "")
    require_auth(cfg.auth_mode, cfg.auth_code_env, provided_code=provided_auth)
    token = getattr(args, "auth_token", "") or os.getenv("AI_AUDIT_AUTH_TOKEN", "")
    if token and args.command in {"repo-run", "ecosystem-run"}:
        enforce_role(token, "admin", f"cli:{args.command}")
    if args.command == "repo-run":
        run_repo_audit()
    elif args.command == "ecosystem-run":
        run_ecosystem_audit()
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
