#!/usr/bin/env python3
"""Build the full agent pack loop: scan → world model → specialist packs.

Usage:
    python scripts/emit_agent_packs.py [--limit 30] [--no-rag] [--quick]

Slim RAG ingest is on by default; disable with --no-rag or AI_AUDIT_PACK_RAG_INGEST=0.
Agent-set pack statuses (claimed/in_progress/resolved/wontfix) survive a re-emit.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.env_loader import load_env_file

load_env_file()

from auditor.agent_pack import (
    emit_packs_from_extraction_candidates,
    emit_packs_from_violations,
    emit_world_model_packs,
    ingest_packs_to_rag,
    list_packs,
    pack_rag_ingest_enabled,
    rebuild_index,
    restore_pack_lifecycle,
    snapshot_pack_lifecycle,
)
from auditor.config import load_architecture_rules, load_ecosystem_config
from auditor.repo_resolver import resolve_repo_path
from auditor.repo_scanner import scan_repo
from conductor.publish import public_base_url
from ecosystem.architecture_engine import ArchitectureEngine
from ecosystem.call_graph_analyzer import analyze_call_graph, violations_from_edges
from ecosystem.graph_builder import GraphBuilder
from patterns.service_role_violations import detect_competing_providers, detect_ui_in_worker


def _load_packs(out: Path) -> list:
    """Reload emitted packs from disk (picks up restored status)."""
    from auditor.agent_pack import AgentCodingPack, load_pack

    packs = []
    for entry in list_packs(out, limit=10_000):
        data = load_pack(out, str(entry.get("id") or ""))
        if data:
            packs.append(AgentCodingPack.from_dict(data))
    return packs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40, help="Max specialist packs from violations")
    parser.add_argument(
        "--ingest-rag",
        action="store_true",
        help="(default; kept for compatibility) ingest slim pack docs into RAG",
    )
    parser.add_argument("--no-rag", action="store_true", help="Skip RAG ingest")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip per-repo call-graph / role scans (graph rules only)",
    )
    parser.add_argument(
        "--no-world",
        action="store_true",
        help="Skip compressed world-model packs",
    )
    args = parser.parse_args()

    cfg = load_ecosystem_config()
    base = public_base_url()
    out = Path(cfg.output_dir)
    ingest_rag = pack_rag_ingest_enabled(False if args.no_rag else True)
    packs_root = out / "agent_packs"
    lifecycle = snapshot_pack_lifecycle(out)
    if packs_root.exists():
        for stale in packs_root.glob("pack-*"):
            stale.unlink(missing_ok=True)
        idx = packs_root / "INDEX.json"
        if idx.exists():
            idx.unlink()

    print("=== 1. Compressed world model ===")
    world: list = []
    if not args.no_world:
        world = emit_world_model_packs(
            output_dir=out,
            run_id="emit-packs-manual",
            public_base_url=base,
            ingest_rag=False,
            include_repos=True,
        )
        print(f"  world_model packs: {len(world)}")

    print("=== 2. Specialist packs (architecture) ===")
    rules = load_architecture_rules()
    graph = GraphBuilder(cfg.graph_db_path)
    try:
        engine = ArchitectureEngine(graph=graph, rules=rules)
        violations = engine.evaluate()
        print(f"  graph rule violations: {len(violations)}")

        if not args.quick:
            repo_files: dict[str, list[Path]] = {}
            for repo in cfg.repos:
                if not repo.enabled:
                    continue
                try:
                    files = scan_repo(repo)
                    if files:
                        repo_files[repo.name] = files
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
                print(f"  call-graph: {len(call_v)}; role: {len(role_v)}")

        repo_paths = {}
        for r in cfg.repos:
            try:
                repo_paths[r.name] = resolve_repo_path(r)
            except Exception:
                continue

        arch = emit_packs_from_violations(
            violations,
            output_dir=out,
            run_id="emit-packs-manual",
            public_base_url=base,
            ingest_rag=False,
            repo_paths=repo_paths,
            limit=args.limit,
        )
        print(f"  architecture packs: {len(arch)}")
    finally:
        graph.close()

    print("=== 3. Specialist packs (extraction) ===")
    extraction = emit_packs_from_extraction_candidates(
        output_dir=out,
        run_id="emit-packs-manual",
        public_base_url=base,
        ingest_rag=False,
        limit=args.limit,
    )
    print(f"  extraction packs: {len(extraction)}")

    restored = restore_pack_lifecycle(out, lifecycle, public_base_url=base)
    if restored:
        print(f"  restored lifecycle status on {restored} packs")

    if ingest_rag:
        # One ingest after status restore so RAG metadata matches the YAML.
        result = ingest_packs_to_rag(_load_packs(out), output_dir=out, full=True)
        print(f"  RAG ingest: {result}")

    idx = rebuild_index(out, public_base_url=base)
    print(
        f"\nDone. total={idx['count']} "
        f"world_model={len(idx.get('layers', {}).get('world_model', []))} "
        f"specialist={len(idx.get('layers', {}).get('specialist', []))} "
        f"kinds={idx.get('kind_counts')}"
    )
    print("World model samples:")
    for entry in list_packs(out, layer="world_model", limit=5):
        print(f"  [world] {entry.get('id')}  {entry.get('title', '')[:60]}")
    print("Specialist samples:")
    for entry in list_packs(out, layer="specialist", limit=8):
        print(
            f"  [{entry.get('kind')}] {entry.get('severity', '?'):8} "
            f"{entry.get('repo', ''):20} {entry.get('id')}"
        )
    from auditor.pack_savings import refresh_quietly

    print(f"Pack savings: {refresh_quietly(out)}")
    print(f"Discover: {base}/audit/packs")
    print(f"Bundle: {base}/audit/packs/bundle?repo=<name>")
    print(f"World model: {base}/audit/packs?kind=world_model")


if __name__ == "__main__":
    main()
