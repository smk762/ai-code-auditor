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
    return parser


def _run_ask(
    query: str,
    as_json: bool = False,
    deep: bool = False,
    iterations: int = 3,
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
        response = engine.ask(query)
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
