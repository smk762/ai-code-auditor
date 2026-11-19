"""Regression tests for wiki/refresh.py audit packs (naive timestamps, lock probing)."""
from __future__ import annotations

import fcntl
import json
import threading
from datetime import datetime, timedelta, timezone

from tests.test_wiki_refresh import _commit, eco, reply  # noqa: F401 — eco is a fixture
from tests.test_wiki import FakeClient
from wiki import refresh as rf
from wiki.batch import Runner


def test_naive_built_at_does_not_crash_the_plan(eco) -> None:  # noqa: F811
    out, repo = eco
    rf.refresh(out, runner=Runner(out, client=FakeClient(reply=reply), poll_s=0), publish=False)
    (repo / "README.md").write_text("# svc\n")
    _commit(repo, "readme")  # the mirror moved, so the interval gate is reached
    p = out / "wiki" / "svc" / "INDEX.json"
    idx = json.loads(p.read_text())
    idx["built_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None).isoformat()
    p.write_text(json.dumps(idx))
    assert rf.plan(out).skipped["svc/internal"] == "built 1d ago (weekly at most)"
    assert rf.plan(out, now=datetime.now()).skipped["svc/internal"].startswith("built ")  # naive `now` too


def test_a_status_probe_never_refuses_a_starting_refresh(tmp_path) -> None:
    out = tmp_path
    with rf._lock(out):
        pass  # creates the lock file
    probe = open(out / "wiki" / ".refresh.lock", "a")
    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)  # a UI poll caught mid-probe
    threading.Timer(0.2, probe.close).start()
    with rf._lock(out):
        assert rf.is_running(out)
    assert not rf.is_running(out)
