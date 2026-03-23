from __future__ import annotations

import sys
from pathlib import Path
import os
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.archive import archive_reports
from auditor.auth import enforce_role
from auditor.config import load_architecture_rules, load_ecosystem_config
from auditor.contracts import CodeUnit
from auditor.gpu_scheduler import GpuScheduler
from auditor.locks import pipeline_lock
from auditor.persistence import (
    completed_repos_for_run,
    recent_findings,
    record_run_finish,
    record_run_start,
    upsert_repo_run,
    write_checkpoint,
)
from auditor.repo_scanner import scan_repo
from auditor.chunker import extract_code_units
from auditor.reporter import (
    write_architecture_violations,
    write_extraction_candidates_markdown,
    write_extraction_candidates_yaml,
)
from auditor.runtime import RunMetadata, require_auth, setup_logging, write_run_metadata
from auditor.retry import with_retries
from auditor.settings import get_settings
from ecosystem.architecture_engine import ArchitectureEngine
from ecosystem.graph_builder import GraphBuilder
from patterns.extraction_candidate_builder import collect_git_metrics, detect_duplicate_clusters
from patterns.pattern_clusterer import cluster_patterns
from patterns.pattern_feedback import write_pattern_report
from patterns.pattern_miner import mine_patterns
from patterns.pattern_proposer import propose_extraction_candidates, propose_patterns
from patterns.pattern_scorer import score_pattern_prevalence
from semantic.embedding_index import EmbeddingIndex


def _write_ecosystem_report(
    output_dir: str,
    unit_count: int,
    violation_count: int,
    proposal_count: int,
    extraction_candidate_count: int,
    repos: list[str],
) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = out / "ecosystem_report.md"
    health_score = max(0, 100 - (violation_count * 5))
    lines = [
        "# Ecosystem Report",
        "",
        f"- Ecosystem Health Score: {health_score}%",
        f"- Repositories scanned: {len(repos)}",
        f"- Code units indexed: {unit_count}",
        f"- Architecture violations: {violation_count}",
        f"- Pattern proposals: {proposal_count}",
        f"- Extraction candidates: {extraction_candidate_count}",
        "",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def run() -> None:
    cfg = load_ecosystem_config()
    setup_logging(level=cfg.log_level, log_file_path=cfg.log_file_path, output_dir=cfg.output_dir)
    rules = load_architecture_rules()

    # Wait until GPU capacity is available before acquiring the pipeline lock.
    # This may block for one or more 30-minute retry cycles.
    scheduler = GpuScheduler()
    gpu = scheduler.wait_for_capacity()

    # AI_AUDIT_RUN_ID lets the audit API pre-assign the run_id so it can
    # insert a DB record and return it to callers before the pipeline starts.
    forced_run_id = os.getenv("AI_AUDIT_RUN_ID", "")
    meta = RunMetadata.start("ecosystem-audit", run_id=forced_run_id)
    meta.extra["gpu_offload_mode"] = gpu.offload_mode
    meta.extra["gpu_num_layers"] = gpu.num_gpu_layers
    meta.extra["gpu_vram_free_mb"] = gpu.vram_free_mb

    all_units: list[CodeUnit] = []
    repo_roots: dict[str, str] = {}
    graph = GraphBuilder(cfg.graph_db_path)
    settings = get_settings()
    try:
        require_auth(cfg.auth_mode, cfg.auth_code_env, provided_code=os.getenv("AI_AUDIT_AUTH_CODE_INPUT", ""))
        auth_token = os.getenv("AI_AUDIT_AUTH_TOKEN", "")
        if auth_token:
            enforce_role(auth_token, "admin", "pipeline:ecosystem-run")
        record_run_start(meta.run_id, "ecosystem-audit")
        with pipeline_lock("ecosystem-audit", ttl_s=7200):
            resume_run_id = os.getenv("AI_AUDIT_RESUME_RUN_ID", "")
            completed = completed_repos_for_run("ecosystem-audit", resume_run_id) if resume_run_id else set()
            t0 = time.monotonic()
            for repo in cfg.repos:
                if repo.name in completed:
                    upsert_repo_run(meta.run_id, repo.name, "skipped", attempts=0, error_message="resumed-skip")
                    continue
                try:
                    files = with_retries(lambda: scan_repo(repo), retries=3, base_delay_s=1.0)
                    if files:
                        repo_roots[repo.name] = str(Path(files[0]).resolve().parent)
                        for parent in Path(files[0]).resolve().parents:
                            if (parent / ".git").exists():
                                repo_roots[repo.name] = str(parent)
                                break
                    for file_path in files:
                        units = extract_code_units(repo.name, file_path)
                        all_units.extend(units)
                    meta.scanned_repos.append(repo.name)
                    meta.scanned_files += len(files)
                    meta.code_units += len(all_units)
                    upsert_repo_run(meta.run_id, repo.name, "completed", attempts=1)
                    write_checkpoint("ecosystem-audit", meta.run_id, repo.name, "completed", {"files": len(files)})
                except Exception as repo_exc:
                    upsert_repo_run(meta.run_id, repo.name, "failed", attempts=1, error_message=str(repo_exc))
                    write_checkpoint("ecosystem-audit", meta.run_id, repo.name, "failed", {"error": str(repo_exc)})
                    meta.add_error(f"{repo.name}: {repo_exc}")
            meta.mark_stage("scan_and_extract", int((time.monotonic() - t0) * 1000), status="partial_success" if meta.errors else "success")

            t1 = time.monotonic()
            graph.upsert_code_units(all_units)
            embedding_index = EmbeddingIndex(cfg.embedding_index_path)
            ok, reason = embedding_index.health_check()
            if not ok and settings.embedder_healthcheck_required:
                raise RuntimeError(f"Embedding provider health check failed: {reason}")
            embedding_index.rebuild(all_units)
            meta.mark_stage("graph_and_embedding_index", int((time.monotonic() - t1) * 1000))

            t2 = time.monotonic()
            mined = mine_patterns(all_units)
            clusters = cluster_patterns(mined)
            scores = score_pattern_prevalence(clusters)
            proposals = propose_patterns(scores)
            write_pattern_report(proposals, output_dir=cfg.output_dir)

            duplicate_clusters = detect_duplicate_clusters(all_units)
            findings = recent_findings(limit=2000)
            git_metrics = {
                cluster["cluster_id"]: collect_git_metrics(cluster, repo_roots)
                for cluster in duplicate_clusters
            }
            extraction_candidates = propose_extraction_candidates(
                duplicate_clusters=duplicate_clusters,
                graph=graph,
                audit_findings=findings,
                git_metrics=git_metrics,
            )
            write_extraction_candidates_yaml(extraction_candidates, output_dir=cfg.output_dir, ecosystem_name="smk-stack")
            write_extraction_candidates_markdown(extraction_candidates, output_dir=cfg.output_dir)
            meta.mark_stage("pattern_mining", int((time.monotonic() - t2) * 1000))

            t3 = time.monotonic()
            engine = ArchitectureEngine(graph=graph, rules=rules)
            violations = engine.evaluate()
            meta.violations = len(violations)
            write_architecture_violations(violations, output_dir=cfg.output_dir)

            _write_ecosystem_report(
                output_dir=cfg.output_dir,
                unit_count=len(all_units),
                violation_count=len(violations),
                proposal_count=len(proposals),
                extraction_candidate_count=len(extraction_candidates),
                repos=[r.name for r in cfg.repos],
            )
            archive_reports(cfg.output_dir, meta.run_id)
            meta.mark_stage("governance_and_reports", int((time.monotonic() - t3) * 1000))
            meta.finish("partial_success" if meta.errors else "success")
    except Exception as exc:
        meta.add_error(str(exc))
        meta.finish("failed")
        raise
    finally:
        scheduler.release_capacity()
        record_run_finish(meta, "ecosystem-audit")
        write_run_metadata(meta, output_dir=cfg.output_dir)
        graph.close()


if __name__ == "__main__":
    run()
