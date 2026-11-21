"""Per-repo bookkeeping in the nightly repo audit: scanned set and attempt counts."""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pipelines.nightly_repo_audit as nightly


def _harness(tmp_path: Path, monkeypatch, audit, head_commit=lambda *_: "abc"):
    import auditor.agent_pack
    import auditor.class_packs
    import auditor.pack_classes
    import auditor.pack_gc
    import auditor.retry
    import auditor.revalidate
    import auditor.test_env_readiness
    import auditor.trust_eval
    import auditor.trust_metrics

    monkeypatch.delenv("AI_AUDIT_LLM_BATCH", raising=False)
    monkeypatch.delenv("AI_AUDIT_AUTH_TOKEN", raising=False)
    cfg = SimpleNamespace(log_level="INFO", log_file_path=None, output_dir=str(tmp_path),
                          repos=[SimpleNamespace(name="r1")], auth_mode=False, auth_code_env="")
    seen: dict = {"repo_runs": [], "stored": None}
    noop = lambda *a, **k: None  # noqa: E731
    monkeypatch.setattr(auditor.retry.time, "sleep", noop)
    monkeypatch.setattr(nightly, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(nightly, "setup_logging", noop)
    monkeypatch.setattr(nightly, "LLMClient", lambda *a, **k: SimpleNamespace(health_check=lambda: (True, "")))
    monkeypatch.setattr(nightly, "get_settings", lambda: SimpleNamespace(llm_healthcheck_required=False))
    monkeypatch.setattr(nightly, "require_auth", noop)
    monkeypatch.setattr(nightly, "pipeline_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(nightly, "run_repo_audit", audit)
    monkeypatch.setattr(nightly, "head_commit", head_commit)
    monkeypatch.setattr(nightly, "resolve_scan_root", lambda r: tmp_path)
    for name in ("record_run_start", "write_checkpoint", "write_nightly_repo_report",
                 "write_security_report", "archive_reports", "write_run_metadata"):
        monkeypatch.setattr(nightly, name, noop)
    monkeypatch.setattr(nightly, "upsert_repo_run",
                        lambda run_id, repo, status, **kw: seen["repo_runs"].append((repo, status, kw)))
    monkeypatch.setattr(nightly, "store_findings", lambda run_id, f: seen.__setitem__("stored", list(f)))
    monkeypatch.setattr(nightly, "dedupe_findings", lambda f: list(f))
    monkeypatch.setattr(nightly, "record_run_finish", lambda meta, _p: seen.__setitem__("meta", meta))
    monkeypatch.setattr(auditor.agent_pack, "emit_packs_from_findings", lambda *a, **k: [])
    for mod, fn in ((auditor.pack_gc, "run_quietly"), (auditor.trust_metrics, "refresh_quietly"),
                    (auditor.pack_classes, "run_quietly"), (auditor.class_packs, "run_quietly"),
                    (auditor.test_env_readiness, "run_quietly"), (auditor.revalidate, "run_quietly"),
                    (auditor.trust_eval, "evaluate_quietly")):
        monkeypatch.setattr(mod, fn, noop)
    return seen


def test_a_failure_after_the_scan_does_not_count_the_repo_as_scanned(tmp_path, monkeypatch) -> None:
    def boom(*_):
        raise OSError("git unavailable")

    seen = _harness(tmp_path, monkeypatch, lambda repo, llm=None, unreviewed=None: ([], ["finding"]),
                    head_commit=boom)
    nightly.run()
    meta = seen["meta"]
    # Before: r1 sat in scanned_repos with none of its findings stored, so GC read it as all fixed.
    assert "r1" not in meta.scanned_repos
    assert seen["stored"] == []
    assert seen["repo_runs"][-1][:2] == ("r1", "failed")


def test_attempts_count_every_retry(tmp_path, monkeypatch) -> None:
    calls = []

    def flaky(repo, llm=None, unreviewed=None):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return [], ["finding"]

    seen = _harness(tmp_path, monkeypatch, flaky)
    nightly.run()
    assert seen["repo_runs"] == [("r1", "completed", {"attempts": 3})]
    assert seen["meta"].scanned_repos == ["r1"]
    assert seen["stored"] == ["finding"]


def test_attempts_on_failure_count_every_retry(tmp_path, monkeypatch) -> None:
    def dead(repo, llm=None, unreviewed=None):
        raise RuntimeError("down")

    seen = _harness(tmp_path, monkeypatch, dead)
    nightly.run()
    repo, status, kw = seen["repo_runs"][-1]
    assert (repo, status, kw["attempts"]) == ("r1", "failed", 3)
