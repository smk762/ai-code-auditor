from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.aggregator import dedupe_findings
from auditor.archive import archive_reports
from auditor.auth import enforce_role
from auditor.config import load_ecosystem_config
from auditor.llm_client import LLMClient
from auditor.locks import pipeline_lock
from auditor.persistence import record_run_finish, record_run_start, store_findings, upsert_repo_run, write_checkpoint
from auditor.repo_auditor import run_repo_audit
from auditor.reporter import write_nightly_repo_report, write_security_report
from auditor.runtime import RunMetadata, require_auth, setup_logging, write_run_metadata
from auditor.retry import with_retries
from auditor.settings import get_settings


def run() -> None:
    cfg = load_ecosystem_config()
    setup_logging(level=cfg.log_level, log_file_path=cfg.log_file_path, output_dir=cfg.output_dir)
    meta = RunMetadata.start("repo-audit")
    all_findings = []
    llm = LLMClient()
    settings = get_settings()
    try:
        require_auth(cfg.auth_mode, cfg.auth_code_env, provided_code=os.getenv("AI_AUDIT_AUTH_CODE_INPUT", ""))
        auth_token = os.getenv("AI_AUDIT_AUTH_TOKEN", "")
        if auth_token:
            enforce_role(auth_token, "admin", "pipeline:repo-run")
        ok, reason = llm.health_check()
        if not ok and settings.llm_healthcheck_required:
            raise RuntimeError(f"LLM health check failed: {reason}")

        record_run_start(meta.run_id, "repo-audit")
        batch_mode = os.getenv("AI_AUDIT_LLM_BATCH", "").strip().lower() in {"1", "true", "yes", "on"}
        with pipeline_lock("repo-audit", ttl_s=8 * 3600 if batch_mode else 7200):
            if batch_mode:
                from auditor.batch_review import prefetch_reviews

                # Review all uncached units via the Batches API first (half price); the
                # per-repo pass below then reads them from the cache and never calls the
                # model synchronously (misses — oversize / over budget / failed — are skipped).
                stats = prefetch_reviews(cfg.repos)
                logger.info("batch prefetch stats: %s", stats)
                os.environ["AI_AUDIT_LLM_CACHE_ONLY"] = "1"
            t0 = time.monotonic()
            for repo in cfg.repos:
                attempts = 0
                try:
                    attempts += 1
                    units, findings = with_retries(lambda: run_repo_audit(repo, llm=llm), retries=3, base_delay_s=1.0)
                    meta.scanned_repos.append(repo.name)
                    meta.scanned_files += len({u.file_path for u in units})
                    meta.code_units += len(units)
                    all_findings.extend(findings)
                    upsert_repo_run(meta.run_id, repo.name, "completed", attempts=attempts)
                    write_checkpoint("repo-audit", meta.run_id, repo.name, "completed", {"units": len(units)})
                except Exception as repo_exc:
                    upsert_repo_run(meta.run_id, repo.name, "failed", attempts=attempts, error_message=str(repo_exc))
                    write_checkpoint("repo-audit", meta.run_id, repo.name, "failed", {"error": str(repo_exc)})
                    meta.add_error(f"{repo.name}: {repo_exc}")
            meta.mark_stage("repo_scan_and_audit", int((time.monotonic() - t0) * 1000), status="partial_success" if meta.errors else "success")

            t1 = time.monotonic()
            deduped = dedupe_findings(all_findings)
            meta.findings = len(deduped)
            write_nightly_repo_report(deduped, output_dir=cfg.output_dir)
            write_security_report(deduped, output_dir=cfg.output_dir)
            store_findings(meta.run_id, deduped)
            try:
                from auditor.agent_pack import emit_packs_from_findings, pack_rag_ingest_enabled
                from auditor.repo_resolver import resolve_repo_path
                from conductor.publish import public_base_url

                repo_paths = {r.name: resolve_repo_path(r) for r in cfg.repos}
                packs = emit_packs_from_findings(
                    deduped,
                    output_dir=cfg.output_dir,
                    kind="nightly",
                    run_id=meta.run_id,
                    public_base_url=public_base_url(),
                    ingest_rag=pack_rag_ingest_enabled(),
                    repo_paths=repo_paths,
                )
                logger.info("emitted %d agent coding packs from nightly findings", len(packs))
            except Exception:
                logger.exception("agent pack emission from nightly findings failed")
            archive_reports(cfg.output_dir, meta.run_id)
            meta.mark_stage("report_generation", int((time.monotonic() - t1) * 1000))
            meta.finish("partial_success" if meta.errors else "success")
    except Exception as exc:
        meta.add_error(str(exc))
        meta.finish("failed")
        raise
    finally:
        record_run_finish(meta, "repo-audit")
        write_run_metadata(meta, output_dir=cfg.output_dir)


if __name__ == "__main__":
    run()
