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
    """A cancel committed while the claim is in flight must win; the run must not come back as running.

    The interleaving is driven, not hoped for: the claim's session is open (the claim has read
    nothing and taken no lock yet) when the cancel runs to completion on its own connection, and
    only then does the claim's statement execute. A claim that doesn't re-check ``pending``
    atomically would overwrite the committed cancel here."""
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
                if not fired:  # an operator cancels while the claim is in flight
                    t = threading.Thread(target=audit_api._db_finish_run_cancelled, args=(run_id,))
                    fired.append(t)
                    t.start()
                    t.join(10)
                    assert not t.is_alive(), "the cancel did not commit inside the claim's window"
                    assert _status(run_id) == "cancelled"  # committed before the claim's statement
                return real_execute(*a, **kw)

            s.execute = execute  # type: ignore[method-assign]
            yield s

    monkeypatch.setattr(audit_api, "db_session", racing_session)
    claimed = audit_api._db_claim_pending_run(run_id)
    assert fired, "the claim ran no statement, so no cancel was raced against it"
    monkeypatch.undo()
    assert claimed is False
    assert _status(run_id) == "cancelled", "the claim overwrote the cancel"


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
def test_push_refuses_refspecs_options_and_urls(monkeypatch: pytest.MonkeyPatch, tmp_path, body: dict) -> None:
    # A configured repo, so only the branch/remote check can refuse; the push itself is a spy.
    pushes = _push_spy(monkeypatch, tmp_path)
    r = TestClient(audit_api.app).post("/audit/git/push", json={"repo": "r1", **body})
    assert r.status_code == 400
    (value,) = body.values()
    assert repr(value) in r.json()["detail"]  # refused for this value, not for something else
    assert pushes == []


def test_push_accepts_plain_branch_and_remote(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    pushes = _push_spy(monkeypatch, tmp_path)
    r = TestClient(audit_api.app).post("/audit/git/push",
                                       json={"repo": "r1", "branch": "repair/fix-1", "remote": "origin"})
    assert r.status_code == 200, r.text
    assert pushes == [{"branch": "repair/fix-1", "remote": "origin"}]


def _push_spy(monkeypatch: pytest.MonkeyPatch, repo_path) -> list[dict]:
    from types import SimpleNamespace

    from auditor import git_ops

    pushes: list[dict] = []

    def push(path, branch=None, remote="origin"):
        pushes.append({"branch": branch, "remote": remote})
        return SimpleNamespace(pushed=True, branch=branch, remote=remote, remote_url="", skipped_reason="")

    _one_repo_config(monkeypatch, repo_path)
    monkeypatch.setattr(git_ops, "push_repair_branch", push)
    return pushes


def _one_repo_config(monkeypatch: pytest.MonkeyPatch, repo_path) -> None:
    from types import SimpleNamespace

    from auditor import repo_resolver

    cfg = SimpleNamespace(repos=[SimpleNamespace(name="r1", enabled=True)], auth_mode=False)
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


# ── _patch_paths: exact names, and a failure is not "no files" ──────────────

def test_patch_paths_returns_real_names(tmp_path) -> None:
    import subprocess

    _git_repo(tmp_path)
    names = ["café.py", "x => y.py", "dir/{a => b}.py", "tab\there.py", "plain.py"]
    for n in names:
        (tmp_path / n).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / n).write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-N", "."], check=True)
    patch = tmp_path.parent / f"{tmp_path.name}.patch"
    patch.write_bytes(subprocess.run(["git", "-C", str(tmp_path), "diff"], capture_output=True, check=True).stdout)
    assert sorted(audit_api._patch_paths(patch, tmp_path)) == sorted(names)


def test_patch_paths_raises_when_git_fails(tmp_path) -> None:
    bad = tmp_path / "bad.patch"
    bad.write_text("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\nthis is not a hunk line\n")
    with pytest.raises(RuntimeError):
        audit_api._patch_paths(bad, tmp_path)


# ── endpoint hardening: webhook, merge switch, diff runs, discovery, ingest, reports ──

def test_webhook_with_non_utf8_body_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_AUDIT_MERGE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("AI_AUDIT_MERGE_HOOK_ENABLED", "1")
    r = TestClient(audit_api.app, raise_server_exceptions=False).post(
        "/audit/hooks/github", content=b"\xff\xfe{", headers={"X-GitHub-Event": "push"})
    assert r.status_code == 400


def test_force_does_not_override_the_merge_hook_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from auditor import merge_hook

    queued: list = []

    class _Sched:
        def enqueue(self, event):
            queued.append(event)
            return {"accepted": True}

    monkeypatch.setattr(merge_hook, "get_scheduler", lambda: _Sched())
    monkeypatch.setenv("AI_AUDIT_MERGE_HOOK_ENABLED", "0")
    r = TestClient(audit_api.app).post("/audit/merge", json={"repo": "x", "force": True})
    assert r.status_code == 503 and queued == []


def test_each_diff_audit_gets_its_own_run_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from types import SimpleNamespace

    from auditor import agent_pack, diff_auditor, repo_resolver

    cfg = SimpleNamespace(repos=[SimpleNamespace(name="r1", enabled=True)], output_dir=str(tmp_path), auth_mode=False)
    monkeypatch.setattr(audit_api, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(audit_api, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(repo_resolver, "resolve_repo_path", lambda repo: tmp_path)
    impact = SimpleNamespace(directly_changed=set(), transitively_affected=set())
    monkeypatch.setattr(diff_auditor, "run_diff_audit", lambda repo, diff: SimpleNamespace(
        repo="r1", findings=[], impact=impact, units_analyzed=[], generated_at="now"))
    seen: list[str] = []

    def emit(findings, **kw):
        seen.append(kw["run_id"])
        return []

    monkeypatch.setattr(agent_pack, "emit_packs_from_findings", emit)
    client = TestClient(audit_api.app)
    a = client.post("/audit/diff", json={"repo": "r1", "diff": "diff --git a/x b/x\n"}).json()
    b = client.post("/audit/diff", json={"repo": "r1", "diff": "diff --git a/y b/y\n"}).json()
    assert len(set(seen)) == 2 and [a["run_id"], b["run_id"]] == seen


def test_pack_discovery_does_not_leak_the_output_dir(client: TestClient, tmp_path) -> None:
    d = client.get("/audit/packs").json()["discovery"]
    assert d["index_file"] == "agent_packs/INDEX.json"
    assert str(tmp_path) not in str(d)


def test_ingest_has_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    seen: dict = {}

    def run(args, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(args, kw.get("timeout") or 0)

    monkeypatch.setattr(audit_api.subprocess, "run", run)
    r = TestClient(audit_api.app, raise_server_exceptions=False).post("/audit/ingest")
    assert seen.get("timeout") and r.status_code == 504


def test_ingest_failure_does_not_return_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    leak = "httpx.ConnectError: http://admin:hunter2@10.0.0.7:6333/ /home/smk/secret"
    monkeypatch.setattr(audit_api.subprocess, "run",
                        lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr=leak))
    r = TestClient(audit_api.app, raise_server_exceptions=False).post("/audit/ingest")
    assert r.status_code == 500 and "hunter2" not in r.text and "/home/smk" not in r.text


def _finished_run(run_id: str, hours_ago: float = 0.0) -> None:
    from datetime import timedelta

    from auditor.db import AuditRun, db_session, utcnow

    with db_session() as s:
        s.add(AuditRun(run_id=run_id, pipeline="ecosystem-audit", status="completed",
                       started_at=utcnow() - timedelta(hours=hours_ago),
                       finished_at=utcnow() - timedelta(hours=hours_ago) + timedelta(minutes=5),
                       metadata_json={}))


def test_report_for_an_older_run_is_gone_once_a_later_run_rewrote_it(client: TestClient, tmp_path) -> None:
    import os
    import time

    _finished_run("eco-old", hours_ago=3)
    sentinel = tmp_path / "run_eco-old.json"
    sentinel.write_text("{}")
    old = time.time() - 3 * 3600 + 300
    os.utime(sentinel, (old, old))
    (tmp_path / "ecosystem_report.md").write_text("# newer run's report")  # written just now
    assert client.get("/audit/reports/eco-old").status_code == 410


def test_report_reads_only_the_runs_known_report_files(client: TestClient, tmp_path) -> None:
    _finished_run("eco-new")
    (tmp_path / "ecosystem_report.md").write_text("# Eco")
    (tmp_path / "scratch_notes.md").write_text("# not a report")
    (tmp_path / "run_eco-new.json").write_text("{}")
    r = client.get("/audit/reports/eco-new")
    assert r.status_code == 200 and "# Eco" in r.text and "not a report" not in r.text
