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
from auditor.repo_resolver import head_commit, resolve_scan_root
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
        prev_cache_only = os.environ.get("AI_AUDIT_LLM_CACHE_ONLY")
        with pipeline_lock("repo-audit", ttl_s=8 * 3600 if batch_mode else 7200):
            if batch_mode:
                from auditor.batch_review import prefetch_reviews

                # Review all uncached units via the Batches API first (half price); the
                # per-repo pass below then reads them from the cache and never calls the
                # model synchronously (misses — oversize / over budget / failed — are skipped).
                stats = prefetch_reviews(cfg.repos)
                logger.info("batch prefetch stats: %s", stats)
                os.environ["AI_AUDIT_LLM_CACHE_ONLY"] = "1"
            try:
                t0 = time.monotonic()
                for repo in cfg.repos:
                    tries = [0]  # every call with_retries makes, not just the first
                    try:
                        unreviewed: set[str] = set()

                        def audit(repo=repo, unreviewed=unreviewed, tries=tries):
                            tries[0] += 1
                            unreviewed.clear()  # a retry starts over
                            return run_repo_audit(repo, llm=llm, unreviewed=unreviewed)
                        units, findings = with_retries(audit, retries=3, base_delay_s=1.0)
                        # Everything that can still fail runs before the repo counts as scanned:
                        # a repo in scanned_repos whose findings never reach store_findings would
                        # read as "all fixed" to pack GC and the trust metrics.
                        unreviewed_rel: list[str] = []
                        if unreviewed:
                            # Silence about these files proves nothing (pack GC, trust metrics).
                            from auditor.trust_metrics import rel_path

                            unreviewed_rel = sorted({rel_path(repo.name, f) for f in unreviewed})
                        # The commit the scan read, so trust metrics only count findings from code
                        # that could contain a fix (the mirrors lag dev until the nightly sync).
                        code = head_commit(resolve_scan_root(repo))
                        n_files = len({u.file_path for u in units})
                        upsert_repo_run(meta.run_id, repo.name, "completed", attempts=tries[0])
                        write_checkpoint("repo-audit", meta.run_id, repo.name, "completed", {"units": len(units)})
                        meta.scanned_repos.append(repo.name)
                        if unreviewed_rel:
                            meta.extra.setdefault("unreviewed_files", {})[repo.name] = unreviewed_rel
                        if code:
                            meta.extra.setdefault("scanned_code", {})[repo.name] = code
                        meta.scanned_files += n_files
                        meta.code_units += len(units)
                        all_findings.extend(findings)
                    except Exception as repo_exc:
                        attempts = tries[0]
                        upsert_repo_run(meta.run_id, repo.name, "failed", attempts=attempts, error_message=str(repo_exc))
                        write_checkpoint("repo-audit", meta.run_id, repo.name, "failed", {"error": str(repo_exc)})
                        meta.add_error(f"{repo.name}: {repo_exc}")
            finally:
                # Cache-only covers the per-repo pass alone: post-run steps (pack classes)
                # need live calls, and a long-lived caller's next run must not inherit it.
                if batch_mode:
                    if prev_cache_only is None:
                        os.environ.pop("AI_AUDIT_LLM_CACHE_ONLY", None)
                    else:
                        os.environ["AI_AUDIT_LLM_CACHE_ONLY"] = prev_cache_only
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
    # After the run is recorded, so this scan counts. GC first (#67), so the metrics see its closures (#52).
    # Neither ever fails the run.
    from auditor.pack_gc import run_quietly as pack_gc
    from auditor.trust_metrics import refresh_quietly as refresh_trust

    logger.info("pack gc: %s", pack_gc(cfg.output_dir))
    # Classes onto new packs (#53): only re-asks packs whose text changed; the classes lane caps it.
    from auditor.pack_classes import run_quietly as classify_packs

    logger.info("pack classes: %s", classify_packs(cfg.output_dir))
    # Then class guard packs (#54): emit, prove recorded guards, close children whose guard holds.
    from auditor.class_packs import run_quietly as class_packs

    logger.info("class packs: %s", class_packs(cfg.output_dir))
    # Which projects validate_patch can run tests for (missing test deps make every fix lint-only).
    from auditor.test_env_readiness import run_quietly as test_env

    logger.info("test env: %s", test_env(cfg.output_dir))
    # Re-check merged fixes that were waived or left unverified, now the test envs may run them (#58).
    # Bounded per night; it only ever records stronger evidence. Before the metrics, which read it.
    from auditor.revalidate import run_quietly as revalidate

    logger.info("revalidate: %s", revalidate(cfg.output_dir))
    logger.info("trust metrics: %s", refresh_trust(cfg.output_dir))
    # Gates last: they read the fresh metrics. The seeded-defect eval re-runs when it's a week old (#63).
    from auditor.trust_eval import evaluate_quietly

    logger.info("trust eval: %s", evaluate_quietly(cfg.output_dir))


if __name__ == "__main__":
    run()
