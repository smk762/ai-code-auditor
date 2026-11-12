#!/usr/bin/env python3
"""Emit agent coding packs from the current architecture engine (no full audit).

Usage:
    python scripts/emit_agent_packs.py [--limit 30] [--ingest-rag] [--quick]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.env_loader import load_env_file

load_env_file()

from auditor.agent_pack import emit_packs_from_violations, list_packs
from auditor.config import load_architecture_rules, load_ecosystem_config
from auditor.repo_resolver import resolve_repo_path
from auditor.repo_scanner import scan_repo
from conductor.publish import public_base_url
from ecosystem.architecture_engine import ArchitectureEngine
from ecosystem.call_graph_analyzer import analyze_call_graph, violations_from_edges
from ecosystem.graph_builder import GraphBuilder
from patterns.service_role_violations import detect_competing_providers, detect_ui_in_worker


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--ingest-rag", action="store_true")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip per-repo call-graph / role scans (graph rules only)",
    )
    args = parser.parse_args()

    cfg = load_ecosystem_config()
    rules = load_architecture_rules()
    graph = GraphBuilder(cfg.graph_db_path)
    try:
        engine = ArchitectureEngine(graph=graph, rules=rules)
        violations = engine.evaluate()
        print(f"Graph rule violations: {len(violations)}")

        if not args.quick:
            repo_files: dict[str, list[Path]] = {}
            for repo in cfg.repos:
                if not repo.enabled:
                    continue
                try:
                    files = scan_repo(repo)
                    if files:
                        repo_files[repo.name] = files
                        print(f"  scanned {repo.name}: {len(files)} files")
                except Exception as exc:
                    print(f"  skip scan {repo.name}: {exc}")

            if repo_files:
                observed = analyze_call_graph(
                    repo_files=repo_files,
                    call_graph_rules=rules.get("call_graph_rules", {}),
                )
                call_v = violations_from_edges(observed)
                role_v = detect_ui_in_worker(
                    repo_files=repo_files,
                    service_roles=rules.get("service_roles", []),
                ) + detect_competing_providers(
                    repo_files=repo_files,
                    service_roles=rules.get("service_roles", []),
                )
                violations = violations + call_v + role_v
                print(f"Call-graph violations: {len(call_v)}; role violations: {len(role_v)}")

        repo_paths = {}
        for r in cfg.repos:
            try:
                repo_paths[r.name] = resolve_repo_path(r)
            except Exception:
                continue

        packs_root = Path(cfg.output_dir) / "agent_packs"
        if packs_root.exists():
            for stale in packs_root.glob("pack-*"):
                stale.unlink(missing_ok=True)
            idx = packs_root / "INDEX.json"
            if idx.exists():
                idx.unlink()

        packs = emit_packs_from_violations(
            violations,
            output_dir=cfg.output_dir,
            run_id="emit-packs-manual",
            public_base_url=public_base_url(),
            ingest_rag=args.ingest_rag,
            repo_paths=repo_paths,
            limit=args.limit,
        )
        print(f"Emitted {len(packs)} packs → {cfg.output_dir}/agent_packs/")
        for entry in list_packs(cfg.output_dir, limit=min(10, args.limit)):
            print(
                f"  {entry.get('severity', '?'):8} {entry.get('repo', ''):22} "
                f"{entry.get('id')}  {entry.get('title', '')[:50]}"
            )
        print(f"Discover: {public_base_url()}/audit/packs")
    finally:
        graph.close()


if __name__ == "__main__":
    main()
