"""Batch mode's cache-only flag covers the per-repo pass and nothing after it."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import pipelines.nightly_repo_audit as nightly


@pytest.mark.parametrize("prev", [None, "0"])
def test_batch_mode_restores_cache_only_after_the_repo_pass(tmp_path: Path, monkeypatch, prev) -> None:
    import auditor.agent_pack
    import auditor.batch_review
    import auditor.class_packs
    import auditor.pack_classes
    import auditor.pack_gc
    import auditor.revalidate
    import auditor.test_env_readiness
    import auditor.trust_eval
    import auditor.trust_metrics

    monkeypatch.setenv("AI_AUDIT_LLM_BATCH", "1")
    if prev is None:
        monkeypatch.delenv("AI_AUDIT_LLM_CACHE_ONLY", raising=False)
    else:
        monkeypatch.setenv("AI_AUDIT_LLM_CACHE_ONLY", prev)
    monkeypatch.delenv("AI_AUDIT_AUTH_TOKEN", raising=False)

    repo = SimpleNamespace(name="r1")
    cfg = SimpleNamespace(
        log_level="INFO", log_file_path=None, output_dir=str(tmp_path), repos=[repo], auth_mode="none", auth_code_env=""
    )
    seen: dict[str, str | None] = {}

    def audit(repo, llm=None):
        seen["repo_pass"] = os.environ.get("AI_AUDIT_LLM_CACHE_ONLY")
        return [], []

    def classify(_out):
        seen["pack_classes"] = os.environ.get("AI_AUDIT_LLM_CACHE_ONLY")
        return {}

    noop = lambda *a, **k: None  # noqa: E731
    monkeypatch.setattr(nightly, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(nightly, "setup_logging", noop)
    monkeypatch.setattr(nightly, "LLMClient", lambda *a, **k: SimpleNamespace(health_check=lambda: (True, "")))
    monkeypatch.setattr(nightly, "get_settings", lambda: SimpleNamespace(llm_healthcheck_required=False))
    monkeypatch.setattr(nightly, "require_auth", noop)
    monkeypatch.setattr(nightly, "pipeline_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(nightly, "run_repo_audit", audit)
    monkeypatch.setattr(nightly, "head_commit", lambda *_: "abc")
    monkeypatch.setattr(nightly, "resolve_scan_root", lambda r: tmp_path)
    for name in (
        "record_run_start", "record_run_finish", "store_findings", "upsert_repo_run", "write_checkpoint",
        "write_nightly_repo_report", "write_security_report", "archive_reports", "write_run_metadata",
    ):
        monkeypatch.setattr(nightly, name, noop)
    monkeypatch.setattr(auditor.batch_review, "prefetch_reviews", lambda repos: {})
    monkeypatch.setattr(auditor.agent_pack, "emit_packs_from_findings", lambda *a, **k: [])
    monkeypatch.setattr(auditor.pack_gc, "run_quietly", noop)
    monkeypatch.setattr(auditor.trust_metrics, "refresh_quietly", noop)
    monkeypatch.setattr(auditor.pack_classes, "run_quietly", classify)
    monkeypatch.setattr(auditor.class_packs, "run_quietly", noop)
    monkeypatch.setattr(auditor.test_env_readiness, "run_quietly", noop)
    monkeypatch.setattr(auditor.revalidate, "run_quietly", noop)
    monkeypatch.setattr(auditor.trust_eval, "evaluate_quietly", noop)

    nightly.run()

    assert seen["repo_pass"] == "1"
    assert seen["pack_classes"] == prev
    assert os.environ.get("AI_AUDIT_LLM_CACHE_ONLY") == prev
