from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor.config import RepoConfig
from tests.test_wiki import APP, FakeClient, responder
from wiki import refresh as rf
from wiki.batch import Runner

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin"}
OTHER = "def helper():\n    return 2\n"


def _commit(repo: Path, msg: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=ENV)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True, env=ENV)


def reply(params: dict) -> str:
    """Like tests.test_wiki.responder, but the interfaces page cites other.py, the rest app.py."""
    if params.get("system") and params["system"][0]["text"].startswith("You write pages"):
        if "**interfaces**" in params["messages"][0]["content"]:
            return "# Interfaces\n\n`helper` is here [other.py:L1-L2].\n"
    return responder(params)


@pytest.fixture
def eco(tmp_path: Path, monkeypatch, memdb) -> tuple[Path, Path]:
    repo = tmp_path / "mirrors" / "svc"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text(APP)
    (repo / "other.py").write_text(OTHER)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=ENV)
    _commit(repo, "init")

    class _Cfg:
        repos = [RepoConfig("svc", str(repo))]
        output_dir = str(tmp_path / "reports")

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    return tmp_path / "reports", repo


def _age_index(out: Path, days: int) -> None:
    p = out / "wiki" / "svc" / "INDEX.json"
    idx = json.loads(p.read_text())
    idx["built_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    p.write_text(json.dumps(idx))


def test_plan_and_incremental_rebuild(eco) -> None:
    out, repo = eco
    fake = FakeClient(reply=reply)
    p = rf.plan(out)
    assert [(i.repo, i.variant, i.pages) for i in p.items] == [("svc", "internal", sorted(["overview", "architecture", "interfaces", "integration"]))]

    rec = rf.refresh(out, runner=Runner(out, client=fake, poll_s=0), publish=False)
    assert sorted(rec["builds"]["internal"]["repos"]["svc"]["pages_written"]) == ["architecture", "integration", "interfaces", "overview"]
    assert rf.plan(out).skipped["svc/internal"] == "up to date with the mirror"

    # a commit that changes only other.py: the mirror moved, but a recent build waits a week
    (repo / "other.py").write_text(OTHER + "\n\ndef more():\n    return 3\n")
    _commit(repo, "other")
    assert rf.plan(out).skipped["svc/internal"].startswith("built 0d ago")
    before = {p["slug"]: p for p in json.loads((out / "wiki" / "svc" / "INDEX.json").read_text())["pages"]}

    _age_index(out, 8)
    p = rf.plan(out)
    assert [(i.pages, i.reasons) for i in p.items] == [(["interfaces"], {"interfaces": "cited file changed: other.py"})]
    calls = len(fake.batches_created)
    rec = rf.refresh(out, runner=Runner(out, client=fake, poll_s=0), publish=False)
    assert rec["builds"]["internal"]["repos"]["svc"]["pages_written"] == ["interfaces"]
    assert len(fake.batches_created) == calls + 3  # the changed file's note, one draft, one check-and-fix
    assert all(len(b) == 1 for b in fake.batches_created[calls:])
    after = {p["slug"]: p for p in json.loads((out / "wiki" / "svc" / "INDEX.json").read_text())["pages"]}
    assert after["overview"] == before["overview"]  # untouched pages keep their entry as is
    assert after["interfaces"]["generated_at"] >= before["interfaces"]["generated_at"]
    assert (out / "wiki" / "svc" / "findings.md").exists()
    assert json.loads((out / "wiki" / "_refresh.json").read_text())["plan"][0]["pages"] == ["interfaces"]


def test_mirror_moved_without_cited_change_and_force(eco) -> None:
    out, repo = eco
    rf.refresh(out, runner=Runner(out, client=FakeClient(reply=reply), poll_s=0), publish=False)
    (repo / "README.md").write_text("# svc\n")
    _commit(repo, "readme")
    _age_index(out, 8)
    assert rf.plan(out).skipped["svc/internal"] == "mirror moved, but no cited file changed"
    assert rf.plan(out, force=True).items[0].pages == sorted(["overview", "architecture", "interfaces", "integration"])


def test_failed_pages_are_retried(eco) -> None:
    out, _repo = eco
    rf.refresh(out, runner=Runner(out, client=FakeClient(reply=reply), poll_s=0), publish=False)
    p = out / "wiki" / "svc" / "INDEX.json"
    idx = json.loads(p.read_text())
    idx["pages"][1]["stale"] = True
    p.write_text(json.dumps(idx))
    plan = rf.plan(out)
    assert plan.items[0].pages == [idx["pages"][1]["slug"]] and "last build failed" in plan.items[0].reasons.values()


def test_one_refresh_at_a_time(eco) -> None:
    out, _repo = eco
    assert not rf.is_running(out)
    with rf._lock(out):
        assert rf.is_running(out)
        with pytest.raises(RuntimeError, match="already running"):
            rf.refresh(out, publish=False, dry_run=True)
    assert not rf.is_running(out)


def test_dry_run_prices_without_writing(eco) -> None:
    out, _repo = eco
    fake = FakeClient(reply=reply)
    rec = rf.refresh(out, runner=Runner(out, client=fake, poll_s=0), dry_run=True)
    assert rec["estimate_usd"] > 0 and not fake.batches_created
    assert not (out / "wiki" / "_refresh.json").exists()


def test_regenerate_endpoint(client: TestClient, eco, monkeypatch) -> None:
    out, _repo = eco
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(out))
    started = []
    monkeypatch.setattr("wiki.refresh.refresh", lambda o, repos, force=False: started.append((repos, force)))
    assert client.post("/audit/wiki/nope/regenerate").status_code == 404
    r = client.post("/audit/wiki/svc/regenerate?force=true")
    assert r.status_code == 202 and r.json()["status"] == "started"
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started == [(["svc"], True)]
    with rf._lock(out):
        assert client.post("/audit/wiki/svc/regenerate").status_code == 409
        assert client.get("/audit/wiki/refresh").json()["running"] is True
    s = client.get("/audit/wiki/refresh").json()
    assert s["running"] is False


def test_public_variant_only_where_needed(eco, monkeypatch, tmp_path) -> None:
    out, repo = eco

    class _Cfg:
        repos = [RepoConfig("argus-svc", str(repo))]
        output_dir = str(out)

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    p = rf.plan(out)
    assert p.skipped["argus-svc/public"] == "internal pages pass the gate (no public variant needed)"
    assert [i.variant for i in p.items] == ["internal"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "wiki_public_report.json").write_text(json.dumps({"refused": [{"repo": "argus-svc", "page": "overview"}]}))
    assert [i.variant for i in rf.plan(out).items] == ["internal", "public"]  # refused last time: build it
    (out / "wiki_public_report.json").unlink()
    assert [i.variant for i in rf.plan(out, all_public=True).items] == ["internal", "public"]
