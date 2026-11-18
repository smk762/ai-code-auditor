"""Regression tests for audit-pack fixes in auditor/audit_api.py."""

from __future__ import annotations

import hmac

import pytest
from fastapi.testclient import TestClient

from auditor import audit_api


# ── auth: secrets are compared in constant time ──────────────────────────────

def test_api_token_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "s3cret")
    monkeypatch.setattr(hmac, "compare_digest", spy)
    audit_api._require_mutating_auth(authorization=None, x_auth_code=None, x_api_token="s3cret")
    assert calls == [(b"s3cret", b"s3cret")]
    with pytest.raises(audit_api.HTTPException) as exc:
        audit_api._require_mutating_auth(authorization=None, x_auth_code=None, x_api_token="s3creX")
    assert exc.value.status_code == 401


def test_auth_code_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    cfg = audit_api.load_ecosystem_config()
    cfg.auth_mode = True
    cfg.auth_code_env = "TEST_AUDIT_AUTH_CODE"
    monkeypatch.setattr(audit_api, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setenv("TEST_AUDIT_AUTH_CODE", "code-1")
    monkeypatch.setattr(hmac, "compare_digest", spy)
    audit_api._require_mutating_auth(authorization=None, x_auth_code="code-1", x_api_token=None)
    assert (b"code-1", b"code-1") in calls


# ── run lifecycle: claim, spawn, cancel, slot, list, stream ─────────────────

def _insert_run(run_id: str, status: str = "pending") -> None:
    from auditor.db import AuditRun, db_session, utcnow

    with db_session() as s:
        s.add(AuditRun(run_id=run_id, pipeline="ecosystem-audit", started_at=utcnow(),
                       status=status, metadata_json={}))


def _status(run_id: str) -> str:
    row = audit_api._db_get_run(run_id)
    assert row is not None
    return row["status"]


def test_claim_never_overwrites_a_cancel_that_lands_mid_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel committed while the claim is in flight must win; the run must not come back as running."""
    import threading
    from contextlib import contextmanager

    from auditor.db import db_session as real_session

    run_id = "eco-claim-race"
    _insert_run(run_id)
    fired: list[threading.Thread] = []

    @contextmanager
    def racing_session():
        with real_session() as s:
            real_execute = s.execute

            def execute(*a, **kw):
                out = real_execute(*a, **kw)
                if not fired:  # an operator cancels right after the claim's first statement
                    t = threading.Thread(target=audit_api._db_finish_run_cancelled, args=(run_id,))
                    fired.append(t)
                    t.start()
                    t.join(0.5)
                return out

            s.execute = execute  # type: ignore[method-assign]
            yield s

    monkeypatch.setattr(audit_api, "db_session", racing_session)
    claimed = audit_api._db_claim_pending_run(run_id)
    fired[0].join(10)
    monkeypatch.undo()
    assert _status(run_id) == "cancelled", f"claim returned {claimed} and overwrote the cancel"


def test_claim_is_single_winner() -> None:
    _insert_run("eco-claim-twice")
    assert audit_api._db_claim_pending_run("eco-claim-twice") is True
    assert audit_api._db_claim_pending_run("eco-claim-twice") is False
    assert _status("eco-claim-twice") == "running"


class _FakeProc:
    def __init__(self, on_init=None, communicate_exc: BaseException | None = None, **kwargs):
        self.kwargs = kwargs
        self.returncode: int | None = None
        self.terminated = self.killed = False
        self._exc = communicate_exc
        if on_init:
            on_init()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self):
        if self._exc is not None:
            raise self._exc
        self.returncode = -15 if self.terminated else 0
        return None, ""


def _spawn_with(monkeypatch: pytest.MonkeyPatch, **fake_kwargs) -> list[_FakeProc]:
    procs: list[_FakeProc] = []

    def popen(args, **kwargs):
        p = _FakeProc(**fake_kwargs, **kwargs)
        procs.append(p)
        return p

    monkeypatch.setattr(audit_api.subprocess, "Popen", popen)
    return procs


def test_cancel_arriving_at_spawn_terminates_the_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "eco-cancel-at-spawn"
    _insert_run(run_id)
    audit_api._cancel_event.clear()
    procs = _spawn_with(monkeypatch, on_init=audit_api._cancel_event.set)
    try:
        audit_api._run_pipeline(run_id)
    finally:
        audit_api._cancel_event.clear()
    assert procs and procs[0].terminated


def test_pipeline_stdout_is_not_buffered(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "eco-devnull"
    _insert_run(run_id)
    audit_api._cancel_event.clear()
    procs = _spawn_with(monkeypatch)
    audit_api._run_pipeline(run_id)
    assert procs[0].kwargs["stdout"] is audit_api.subprocess.DEVNULL
    assert _status(run_id) == "completed"


def test_child_is_killed_when_communicate_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "eco-orphan"
    _insert_run(run_id)
    audit_api._cancel_event.clear()
    procs = _spawn_with(monkeypatch, communicate_exc=MemoryError())
    try:
        audit_api._run_pipeline(run_id)
    except MemoryError:
        pass
    assert procs[0].killed
    assert audit_api._active_process is None and audit_api._active_run_id is None


def test_failed_pending_insert_frees_the_run_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(run_id: str) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_api, "_db_insert_pending", boom)
    monkeypatch.setattr(audit_api, "_active_run_id", None)
    client = TestClient(audit_api.app, raise_server_exceptions=False)
    assert client.post("/audit/run").status_code == 500
    assert audit_api._active_run_id is None


def test_list_runs_never_exceeds_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"run_id": f"r{i}", "status": "completed"} for i in range(3)]
    monkeypatch.setattr(audit_api, "_db_list_runs", lambda limit: rows[:limit])
    monkeypatch.setattr(audit_api, "_db_get_run", lambda rid: {"run_id": rid, "status": "running"})
    monkeypatch.setattr(audit_api, "_active_run_id", "live")
    out = TestClient(audit_api.app).get("/audit/runs?limit=3").json()
    assert len(out) == 3 and out[0]["run_id"] == "live"


def test_stream_ends_after_its_maximum_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run stuck in ``running`` must not hold a stream (and a worker) open forever."""
    import asyncio

    class _Req:
        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr(audit_api, "_db_get_run", lambda rid: {"run_id": rid, "status": "running"})
    monkeypatch.setattr(audit_api, "STREAM_MAX_S", 0.0, raising=False)
    resp = audit_api.stream_run(run_id="stuck", request=_Req(), interval=0.25)
    it = resp.body_iterator
    assert hasattr(it, "__anext__"), "stream generator must be async (not hold a threadpool worker)"

    async def drain() -> list[str]:
        out: list[str] = []
        async for chunk in it:
            out.append(chunk)
            if len(out) > 20:
                break
        return out

    events = asyncio.run(asyncio.wait_for(drain(), 10))
    assert len(events) <= 20 and events[-1].startswith("event: timeout")


# ── git arguments from requests or config never become options ──────────────

@pytest.mark.parametrize("body", [
    {"branch": "+main"},
    {"branch": "+HEAD:refs/heads/main"},
    {"branch": ":main"},
    {"branch": "--force"},
    {"branch": "a..b"},
    {"remote": "https://evil.example/x.git"},
    {"remote": "--receive-pack=touch /tmp/pwned"},
    {"remote": "../other"},
])
def test_push_refuses_refspecs_options_and_urls(body: dict) -> None:
    r = TestClient(audit_api.app).post("/audit/git/push", json={"repo": "no-such-repo", **body})
    assert r.status_code == 400


def test_push_accepts_plain_branch_and_remote() -> None:
    r = TestClient(audit_api.app).post("/audit/git/push",
                                       json={"repo": "no-such-repo", "branch": "repair/fix-1", "remote": "origin"})
    assert r.status_code == 404  # got past validation to the repo lookup


def _one_repo_config(monkeypatch: pytest.MonkeyPatch, repo_path) -> None:
    from types import SimpleNamespace

    from auditor import repo_resolver

    cfg = SimpleNamespace(repos=[SimpleNamespace(name="r1", enabled=True)])
    monkeypatch.setattr(audit_api, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(repo_resolver, "resolve_repo_path", lambda repo: repo_path)


def test_repos_status_reports_a_broken_repo_instead_of_failing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from auditor import git_ops

    _one_repo_config(monkeypatch, tmp_path)

    def broken(path):
        raise git_ops.GitError("not a git repository")

    monkeypatch.setattr(git_ops, "get_current_branch", broken)
    r = TestClient(audit_api.app, raise_server_exceptions=False).get("/audit/repos/status")
    assert r.status_code == 200
    assert "not a git repository" in r.json()[0]["diff_error"]


def _git_repo(path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "x"], check=True)


def test_repos_status_never_passes_an_option_as_the_base(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from auditor import git_ops, repo_resolver

    _git_repo(tmp_path)
    _one_repo_config(monkeypatch, tmp_path)
    monkeypatch.setattr(git_ops, "get_current_branch", lambda p: "dev")
    monkeypatch.setattr(git_ops, "is_working_tree_clean", lambda p: True)
    monkeypatch.setattr(git_ops, "is_in_conflict_state", lambda p: False)
    target = tmp_path / "clobbered"
    monkeypatch.setattr(repo_resolver, "select_branch", lambda repo, path: f"--output={target}")
    r = TestClient(audit_api.app, raise_server_exceptions=False).get("/audit/repos/status")
    assert not target.exists()
    assert r.status_code == 200 and "invalid git ref" in r.json()[0]["diff_error"]
