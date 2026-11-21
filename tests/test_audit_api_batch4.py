"""audit_api: wiki regenerate claim, raw-diff status codes, late cancels, RAG refresh after a pack update."""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auditor import audit_api


def _one_repo(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from auditor import config, repo_resolver

    cfg = SimpleNamespace(repos=[SimpleNamespace(name="r1", enabled=True)], auth_mode=False,
                          output_dir=str(tmp_path))
    monkeypatch.setattr(audit_api, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(config, "load_ecosystem_config", lambda *a, **k: cfg)
    monkeypatch.setattr(repo_resolver, "resolve_repo_path", lambda repo: tmp_path)


# ── wiki regenerate: one claim at a time ─────────────────────────────────────

def test_second_regenerate_is_refused_until_the_first_ends(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import wiki.refresh

    _one_repo(monkeypatch, tmp_path)
    monkeypatch.setattr(audit_api, "_output_dir", lambda: str(tmp_path))
    release, started, done = threading.Event(), threading.Event(), threading.Event()

    def slow_refresh(out, repos, force=False):
        started.set()
        release.wait(10)
        done.set()

    # Before refresh() takes its file lock, is_running() can't see the first request.
    monkeypatch.setattr(wiki.refresh, "is_running", lambda out: False)
    monkeypatch.setattr(wiki.refresh, "refresh", slow_refresh)
    client = TestClient(audit_api.app)
    assert client.post("/audit/wiki/r1/regenerate").status_code == 202
    assert client.post("/audit/wiki/r1/regenerate").status_code == 409
    assert started.wait(5)
    release.set()
    assert done.wait(5)
    for _ in range(100):  # the claim is released in the thread's finally, just after refresh returns
        with audit_api._wiki_regen_lock:
            if not audit_api._wiki_regen_claimed:
                break
        threading.Event().wait(0.02)
    done.clear()
    assert client.post("/audit/wiki/r1/regenerate").status_code == 202  # free again
    assert done.wait(5)


# ── /audit/git/diff: every error is non-2xx ──────────────────────────────────

@pytest.mark.parametrize("outcome, status, reason", [
    (subprocess.TimeoutExpired(cmd="git", timeout=30), 504, "timeout"),
    (SimpleNamespace(returncode=128, stdout="", stderr="fatal: bad revision 'nope...HEAD'"), 422, "branch_not_found"),
    (SimpleNamespace(returncode=128, stdout="", stderr="fatal: not a git repository"), 502, "git_error"),
])
def test_raw_diff_failures_are_not_200(monkeypatch: pytest.MonkeyPatch, tmp_path, outcome, status, reason) -> None:
    _one_repo(monkeypatch, tmp_path)

    def run(*a, **k):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(audit_api.subprocess, "run", run)
    r = TestClient(audit_api.app).post("/audit/git/diff", json={"repo": "r1", "compare_branch": "nope"})
    assert r.status_code == status
    assert r.json()["reason"] == reason


def test_raw_diff_success_is_200(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _one_repo(monkeypatch, tmp_path)
    monkeypatch.setattr(audit_api.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="diff --git a/x b/x\n", stderr=""))
    r = TestClient(audit_api.app).post("/audit/git/diff", json={"repo": "r1", "compare_branch": "dev"})
    assert r.status_code == 200 and r.json()["is_empty"] is False


def test_bad_ref_is_a_documented_reason() -> None:
    assert '"bad_ref"' in (audit_api.git_raw_diff.__doc__ or "")


# ── a cancel landing after a clean exit doesn't relabel the run ──────────────

def test_cancel_after_clean_exit_still_records_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    from auditor.db import AuditRun, db_session, utcnow

    run_id = "eco-late-cancel"
    with db_session() as s:
        s.add(AuditRun(run_id=run_id, pipeline="ecosystem-audit", started_at=utcnow(),
                       status="pending", metadata_json={}))

    class Proc:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):  # on a process that already exited: a no-op
            pass

        def communicate(self):
            self.returncode = 0
            # the operator's cancel lands after the pipeline exited, before the slot is cleared
            audit_api._db_finish_run_cancelled(run_id, "Cancelled by operator.")
            audit_api._cancel_event.set()
            return None, ""

    monkeypatch.setattr(audit_api.subprocess, "Popen", lambda *a, **k: Proc())
    audit_api._cancel_event.clear()
    try:
        audit_api._run_pipeline(run_id)
    finally:
        audit_api._cancel_event.clear()
    assert audit_api._db_get_run(run_id)["status"] == "completed"


# ── a RAG outage after the status is saved doesn't report "not updated" ──────

def _pack_update(monkeypatch: pytest.MonkeyPatch, tmp_path, updated, rag):
    import auditor.agent_pack
    import conductor.publish

    calls: list = []
    monkeypatch.setattr(audit_api, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(audit_api, "_load_pack_for_repo", lambda pid, repo: ({"id": pid}, ""))
    monkeypatch.setattr(auditor.agent_pack, "update_pack_status", lambda *a, **k: updated)
    monkeypatch.setattr(auditor.agent_pack, "refresh_pack_rag", lambda out, p: (calls.append(p), rag(p)))
    monkeypatch.setattr(conductor.publish, "public_base_url", lambda: "")
    return calls


def test_rag_failure_after_the_update_still_reports_updated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    def down(_p):
        raise ConnectionError("qdrant down")

    _pack_update(monkeypatch, tmp_path, {"id": "p1", "status": "resolved"}, down)
    res = audit_api._resolve_pack_after_validation("p1", repo="r1", tool="pytest")
    assert res["updated"] is True and res["status"] == "resolved"
    assert res["rag_refreshed"] is False


def test_no_rag_refresh_when_nothing_was_updated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls = _pack_update(monkeypatch, tmp_path, None, lambda p: None)
    res = audit_api._resolve_pack_after_validation("p1", repo="r1", tool="pytest", expect=frozenset({"claimed"}))
    assert res["updated"] is False
    assert calls == []
