"""Trust metrics (#52): false-resolve, reopen, recurrence, scan persistence, agent PR reverts, flow."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from auditor import trust_metrics as tm
from auditor.agent_pack import emit_packs_from_findings, load_pack, update_pack_status
from auditor.contracts import Finding
from auditor.db import AuditRun, FindingRecord

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


def pack(pid: str, *, title: str = "Bug", path: str = "/m/x/app.py", line: int = 10, status: str = "open",
         history: list[tuple[str, float, str]] = (), kind: str = "nightly", detected: float = 0, run: str = "r0",
         repo: str = "x", **gov) -> tm.PackView:
    return tm.pack_view({
        "work_item_id": pid, "kind": kind, "status": status,
        "repo": {"name": repo}, "location": {"file_path": path, "line": line},
        "narrative": {"title": title}, "provenance": {"detected_at": at(detected), "run_id": run},
        "governance": gov,
        "status_history": [{"status": s, "at": at(h), "note": n} for s, h, n in history],
    })


def scan(run: str, hours: float, findings: list[tuple[str, str, int]] = (), repo: str = "x", ftype: str = "bug") -> tm.Scan:
    return tm.Scan(run_id=run, repo=repo, at=T0 + timedelta(hours=hours),
                   findings=[{"path": p, "title": t, "line": ln, "type": ftype} for p, t, ln in findings])


LINT = "validate_patch passed (repo=x, tool=lint_only)"
PYTEST = "validate_patch passed (repo=x, tool=pytest)"


# ── false resolve / reopen ───────────────────────────────────────────────────

def test_false_resolve_counts_validator_tools_and_manual() -> None:
    packs = [
        pack("a", status="resolved", history=[("claimed", 1, ""), ("resolved", 2, LINT)]),
        pack("b", status="resolved", history=[("claimed", 1, ""), ("resolved", 2, PYTEST)]),
        pack("c", status="resolved", history=[("resolved", 2, "Already fixed on dev by #9")]),
        pack("d", status="resolved", repo="y", history=[("resolved", 2, "validate_patch passed (repo=y, tool=pytest+npm_checks)")]),
    ]
    m = tm.false_resolve_metric(tm.resolution_events(packs))
    assert (m["resolutions"], m["validator"], m["lint_only"], m["manual"]) == (4, 3, 1, 1)
    assert m["rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["methods"]["validator:pytest+npm_checks"] == 1
    assert m["by_repo"]["x"]["rate"] == 0.5 and m["by_repo"]["y"]["rate"] == 0.0
    assert (m["since_bar"], m["rate_since_bar"]) == (0, None)


def test_false_resolve_since_bar_leaves_out_pre_bar_resolutions() -> None:
    bar = "validate_patch passed (repo=x, tool={}, evidence={})"
    packs = [pack(c, status="resolved", history=[("resolved", 2, LINT)]) for c in "abc"]  # before the bar
    packs += [pack("d", status="resolved", history=[("resolved", 3, bar.format("pytest", "repro"))]),
              pack("e", status="resolved", history=[("resolved", 3, bar.format("lint_only", "waiver"))])]
    m = tm.false_resolve_metric(tm.resolution_events(packs))
    assert m["rate"] == pytest.approx(4 / 5, abs=1e-4)
    assert (m["since_bar"], m["lint_only_since_bar"], m["rate_since_bar"]) == (2, 1, 0.5)


def test_resolved_twice_is_two_resolutions_but_repeats_are_one() -> None:
    p = pack("a", status="resolved", history=[
        ("resolved", 1, LINT), ("resolved", 1.5, LINT),  # same resolution re-sent
        ("claimed", 2, "came back"), ("resolved", 3, PYTEST),
    ])
    events = tm.resolution_events([p])
    assert [e["method"] for e in events] == ["validator:lint_only", "validator:pytest"]
    r = tm.reopen_metric([p])
    assert (r["ever_resolved"], r["reopened"], r["ids"]) == (1, 1, ["a"])


def test_reopen_ignores_wontfix_and_never_resolved() -> None:
    packs = [
        pack("a", history=[("claimed", 1, ""), ("wontfix", 2, "")]),
        pack("b", history=[("resolved", 1, ""), ("wontfix", 2, "superseded")]),
        pack("c", history=[("claimed", 1, ""), ("open", 2, "")]),
    ]
    r = tm.reopen_metric(packs)
    assert (r["ever_resolved"], r["reopened"], r["rate"]) == (1, 0, 0.0)


# ── recurrence ───────────────────────────────────────────────────────────────

def test_recurrence_pending_until_a_later_scan() -> None:
    p = pack("a", title="Missing timeout on HTTP call", status="resolved", history=[("resolved", 5, PYTEST)])
    r = tm.recurrence_metric([p], [scan("s1", 1, [("app.py", "Missing timeout on HTTP call", 10)])])
    assert (r["observed"], r["pending"], r["rate"]) == (0, 1, None)
    r = tm.recurrence_metric([p], [scan("s2", 6, [])])
    assert (r["observed"], r["regressed"], r["rate"]) == (1, 0, 0.0)


def test_recurrence_from_later_scan_reworded_and_moved_line() -> None:
    p = pack("a", title="Missing timeout on outbound HTTP call", line=10, status="resolved",
             history=[("resolved", 5, LINT)])
    r = tm.recurrence_metric([p], [scan("s2", 6, [("app.py", "Outbound HTTP call has no timeout", 40)])])
    assert (r["observed"], r["regressed"]) == (1, 1)
    assert r["examples"][0]["evidence"].startswith("scan s2")


def test_finding_moved_to_another_file_is_not_a_recurrence() -> None:
    p = pack("a", title="Missing timeout on HTTP call", status="resolved", history=[("resolved", 5, PYTEST)])
    r = tm.recurrence_metric([p], [scan("s2", 6, [("other.py", "Missing timeout on HTTP call", 10)])])
    assert (r["observed"], r["regressed"]) == (1, 0)


def test_recurrence_link_is_definitive_and_one_finding_explains_one_resolution() -> None:
    a = pack("a", title="Client-controlled base URL enables SSRF", status="resolved", history=[("resolved", 5, PYTEST)])
    b = pack("b", title="Client-controlled callback URL enables SSRF", line=40, status="resolved",
             history=[("resolved", 5, PYTEST)])
    new = pack("n", title="Client-controlled base URL enables SSRF", detected=8, run="r9")
    r = tm.recurrence_metric([a, b, new], [scan("s2", 8, [])])
    assert r["regressed"] == 1  # n explains a (exact title), not also b
    assert [e["pack_id"] for e in r["examples"]] == ["a"]

    linked = pack("m", title="Totally reworded", detected=8, run="r9", recurrence_of="b")
    r = tm.recurrence_metric([a, b, linked], [scan("s2", 8, [])])
    assert {e["pack_id"]: e["evidence"] for e in r["examples"]} == {"b": "pack m (recurrence_of)"}


def test_same_run_pack_is_not_evidence() -> None:
    a = pack("a", title="Pickle loaded unsafely", status="resolved", run="r1", history=[("resolved", 5, PYTEST)])
    twin = pack("t", title="Pickle loaded unsafely", run="r1", detected=6)
    r = tm.recurrence_metric([a, twin], [scan("s2", 8, [])])
    assert r["regressed"] == 0


def test_disputed_vs_regressed() -> None:
    a = pack("a", title="Coverage metric computed for unlabelled items", status="resolved",
             history=[("resolved", 5, "Already handled: tag_coverage returns None")])
    b = pack("b", title="Lock file followed through a symlink", path="/m/x/lock.py", status="resolved",
             history=[("resolved", 5, PYTEST)])
    s = scan("s2", 6, [("app.py", "Coverage metric computed for unlabelled items", 10),
                       ("lock.py", "Lock file followed through a planted symlink", 10)])
    r = tm.recurrence_metric([a, b], [s])
    assert (r["observed"], r["regressed"], r["disputed"]) == (2, 1, 1)
    assert r["rate"] == 0.5 and r["dispute_rate"] == 0.5
    assert {e["pack_id"]: e["kind"] for e in r["examples"]} == {"a": "disputed", "b": "regressed"}


def test_non_finding_kinds_are_ignored() -> None:
    p = pack("w", kind="world_model", status="resolved", history=[("resolved", 5, "")])
    assert tm.recurrence_metric([p], [scan("s2", 6, [])])["observed"] == 0


def test_scan_of_code_older_than_the_resolution_is_not_evidence() -> None:
    """The mirrors lag dev until the nightly sync: a scan that ran after a fix but read older
    code (and the packs it emitted) can't show the fix didn't hold (2026-11-17: 48 false
    regressions, every one from a scan of code committed before the resolution)."""
    p = pack("a", title="Missing timeout on HTTP call", status="resolved", history=[("resolved", 5, PYTEST)])
    stale = scan("r9", 8, [("app.py", "Missing timeout on HTTP call", 10)])
    stale.code_at = T0 + timedelta(hours=1)
    linked = pack("n", title="Missing timeout on HTTP call", detected=8, run="r9", recurrence_of="a")
    r = tm.recurrence_metric([p, linked], [stale])
    assert (r["observed"], r["regressed"], r["pending"]) == (0, 0, 1)

    fresh = scan("r10", 9, [("app.py", "Missing timeout on HTTP call", 10)])
    fresh.code_at = T0 + timedelta(hours=6)
    r = tm.recurrence_metric([p, linked], [stale, fresh])
    assert (r["observed"], r["regressed"]) == (1, 1)
    assert r["examples"][0]["evidence"].startswith("scan r10")


def test_unknown_code_time_falls_back_to_the_scan_start() -> None:
    p = pack("a", title="Missing timeout on HTTP call", status="resolved", history=[("resolved", 5, PYTEST)])
    r = tm.recurrence_metric([p], [scan("s2", 6, [("app.py", "Missing timeout on HTTP call", 10)])])
    assert (r["observed"], r["regressed"]) == (1, 1)


def test_load_scans_reads_the_recorded_commit(memdb) -> None:
    from auditor import ui

    with memdb() as s:
        s.add(AuditRun(run_id="repo-1", pipeline="repo-audit", status="success", started_at=T0 + timedelta(hours=10),
                       metadata_json={"scanned_repos": ["x", "y"], "extra": {"scanned_code": {
                           "x": {"sha": "abc", "committed_at": at(2)}}}}))
        s.add(FindingRecord(run_id="repo-1", repo="x", file_path="/home/smk/x/app.py", line=3,
                            finding_type="bug", severity="HIGH", title="t", source="llm", created_at=T0))
    with ui._db_session() as s:
        scans = {sc.repo: sc for sc in tm.load_scans(s, since=T0 - timedelta(days=1))}
    assert scans["x"].code_at == T0 + timedelta(hours=2) and scans["x"].code_time == T0 + timedelta(hours=2)
    assert scans["y"].code_at is None and scans["y"].code_time == T0 + timedelta(hours=10)


def _git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(cwd), "PATH": "/usr/bin:/bin"}
    if date:
        env |= {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


def test_code_time_comes_from_the_mirror_reflog_and_head_commit(tmp_path: Path) -> None:
    from auditor.repo_resolver import head_commit

    repo = tmp_path / "x"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "one", date="2026-11-01T02:00:00+00:00")
    code = head_commit(repo)
    assert code["sha"] == _git(repo, "rev-parse", "HEAD")
    assert tm._parse_ts(code["committed_at"]) == datetime(2026, 11, 1, 2, tzinfo=UTC)  # git may write Z or +00:00
    assert head_commit(tmp_path / "nope") == {}

    now = datetime.now(UTC)
    later, before_reflog = scan("s", 0), scan("s0", 0)
    later.at, before_reflog.at = now + timedelta(minutes=5), now - timedelta(days=30)
    recorded = scan("s1", 0)
    recorded.code_at = T0
    tm.attach_code_times([later, before_reflog, recorded], {"x": repo})
    assert later.code_at == datetime(2026, 11, 1, 2, tzinfo=UTC)  # the commit's time, not the scan's
    assert before_reflog.code_at is None  # the reflog doesn't reach back: unknown, not the oldest entry
    assert recorded.code_at == T0  # the run's own record wins


# ── scan persistence and flow ────────────────────────────────────────────────

def test_scan_persistence_excludes_findings_resolved_in_between() -> None:
    s1 = scan("s1", 0, [("app.py", "Race on cache write", 1), ("app.py", "Pickle loaded unsafely", 5), ("b.py", "Flaky", 1)])
    s2 = scan("s2", 10, [("app.py", "Race on cache write", 2)])
    fixed = pack("f", title="Pickle loaded unsafely", line=5, history=[("resolved", 5, PYTEST)], status="resolved")
    r = tm.scan_persistence_metric([s1, s2], [fixed])
    assert (r["findings"], r["refound"]) == (2, 1)  # the pickle finding is left out; "Flaky" vanished
    assert r["rate"] == 0.5


def test_flow_pairs_rewordings_and_counts_by_type() -> None:
    s1 = scan("s1", 0, [("app.py", "Model unloaded while inference running", 10), ("app.py", "Gone bug", 50)])
    s2 = scan("s2", 24, [("app.py", "Model unloaded during running inference", 11), ("c.py", "New bug", 1)])
    flow = tm.flow_metric([s1, s2])
    [(_week, f)] = flow.items()
    assert f == {"new": {"bug": 1}, "gone": {"bug": 1}}


def test_load_scans_skips_runs_that_store_no_findings(memdb) -> None:
    """Ecosystem runs list scanned_repos but store no findings; they are not empty scans."""
    from auditor import ui

    with memdb() as s:
        for rid, pipe, h in [("repo-1", "repo-audit", 0), ("eco-1", "ecosystem-audit", 5), ("repo-2", "repo-audit", 10),
                             ("repo-fail", "repo-audit", 12)]:
            s.add(AuditRun(run_id=rid, pipeline=pipe, status="failed" if "fail" in rid else "success",
                           started_at=T0 + timedelta(hours=h), metadata_json={"scanned_repos": ["x", "y"]}))
        s.add(FindingRecord(run_id="repo-1", repo="x", file_path="/home/smk/mirrors/x/app.py", line=3,
                            finding_type="bug", severity="HIGH", title="t", source="llm", created_at=T0))
        s.add(FindingRecord(run_id="repo-2", repo="x", file_path="/home/smk/x/app.py", line=3,
                            finding_type="bug", severity="HIGH", title="t", source="llm", created_at=T0))
    with ui._db_session() as s:
        scans = tm.load_scans(s, since=T0 - timedelta(days=1))
        assert tm.load_scans(s, since=T0 + timedelta(hours=11)) == []  # lookback window
    assert [(sc.run_id, sc.repo, len(sc.findings)) for sc in scans] == [
        ("repo-1", "x", 1), ("repo-1", "y", 0), ("repo-2", "x", 1), ("repo-2", "y", 0)]
    assert {f["path"] for sc in scans for f in sc.findings} == {"app.py"}  # mirror vs home checkout
    assert tm.scan_persistence_metric(scans, [])["rate"] == 1.0


# ── agent PRs ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,body,expected", [
    ("fix: resolve 21 ai-code-auditor findings (races, safety)", "", True),
    ("Fix 12 ai-code-auditor findings", "", True),
    ("fix: ai-code-auditor nightly — 43 packs (41 resolved)", "", True),
    ("fix(miso): unload on idle", "Resolves pack-voluptas-0123456789", True),
    ("docs: add ai-code-auditor agent pack awareness", "pack-x-0123456789", False),
    ("Release sync: dev → main", "fixes ai-code-auditor findings", False),
    ("feat: new page", "mentions ai-code-auditor findings in passing", False),
])
def test_is_agent_pr(title: str, body: str, expected: bool) -> None:
    assert tm.is_agent_pr(title, body) is expected


PR = {"repo": "x", "slug": "o/x", "number": 7, "title": "fix: resolve 3 ai-code-auditor findings",
      "merged_at": at(0), "merge_sha": "m" * 40, "commit_shas": ["c" * 40]}


def test_followups_revert_and_fix_forward_and_window() -> None:
    commits = [
        ("r1", at(24), 'Revert "fix: resolve 3 ai-code-auditor findings"\n\nThis reverts commit ' + "m" * 40 + "."),
        ("f1", at(30), "fix: follow-up for #7 edge case"),
        ("late", at(24 * 20), "Revert #7"),
        ("other", at(10), "fix: unrelated #70"),
        ("c" * 40, at(-1), "the PR's own commit"),
    ]
    assert tm.classify_followups(PR, commits) == {"reverts": ["r1"], "fixes": ["f1"]}


def test_revert_of_a_revert_counts_once_against_the_original() -> None:
    commits = [
        ("r1", at(24), "Revert \"fix: resolve 3 ai-code-auditor findings\"\n\nThis reverts commit " + "m" * 40),
        ("r2", at(48), 'Revert "Revert "fix: resolve 3 ai-code-auditor findings""\n\nThis reverts commit r1.'),
    ]
    assert tm.classify_followups(PR, commits)["reverts"] == ["r1"]
    m = tm.agent_pr_metric([PR], lambda repo, since: commits, now=T0 + timedelta(days=30))
    assert (m["merged"], m["observed"], m["mature"], m["reverted"], m["rate"], m["mature_rate"]) == (1, 1, 1, 1, 1.0, 1.0)


def test_agent_pr_without_mirror_is_unobserved() -> None:
    def no_mirror(repo: str, since: str):
        raise FileNotFoundError(repo)
    m = tm.agent_pr_metric([PR], no_mirror, now=T0 + timedelta(days=1))
    assert (m["merged"], m["observed"], m["rate"]) == (1, 0, None)
    assert m["prs"][0]["mature"] is False


def test_mirror_git_log_reads_real_commits(tmp_path: Path) -> None:
    repo = tmp_path / "x"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "dev")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "fix: one\n\nbody #7")
    log = tm.mirror_git_log({"x": repo})("x", "2000-01-01")
    assert len(log) == 1 and log[0][2].startswith("fix: one") and "#7" in log[0][2]


def test_fetch_agent_prs_filters_and_paginates() -> None:
    def node(n: int, title: str, body: str = "", merged: bool = True, repo: str = "x") -> dict:
        return {"number": n, "title": title, "body": body, "url": f"https://g/{repo}/{n}",
                "mergedAt": at(n) if merged else None, "repository": {"name": repo, "nameWithOwner": f"o/{repo}"},
                "mergeCommit": {"oid": f"m{n}"}, "commits": {"nodes": [{"commit": {"oid": f"c{n}"}}]}}
    pages = {
        None: {"pageInfo": {"hasNextPage": True, "endCursor": "p2"},
               "nodes": [node(1, "Fix 2 ai-code-auditor findings"), node(2, "docs: ai-code-auditor packs")]},
        "p2": {"pageInfo": {"hasNextPage": False}, "nodes": [node(3, "fix: x", "pack-x-0123456789"),
                                                            node(4, "Fix ai-code-auditor findings", repo="elsewhere")]},
    }
    calls = []

    def gql(query: str, variables: dict) -> dict:
        calls.append(variables)
        return {"search": pages[variables["cursor"]] if "ai-code-auditor" in variables["q"] else
                {"pageInfo": {"hasNextPage": False}, "nodes": [node(3, "fix: x", "pack-x-0123456789")]}}

    prs = tm.fetch_agent_prs(gql, "o", repos={"x"})
    assert [(p["number"], p["pack_ids"]) for p in prs] == [(1, []), (3, ["pack-x-0123456789"])]
    assert prs[0]["commit_shas"] == ["c1"] and prs[0]["merge_sha"] == "m1"


# ── end to end ───────────────────────────────────────────────────────────────

def test_refresh_writes_report_and_appends_history(tmp_path: Path, memdb, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    f = Finding(id="llm:u:1", type="bug", severity="HIGH", repo="x", file_path="/m/x/app.py", line=10,
                title="Missing timeout on HTTP call", description="d", evidence="e", recommendation="r", source="llm")
    [p] = emit_packs_from_findings([f], output_dir=tmp_path, kind="nightly", run_id="run-1")
    update_pack_status(tmp_path, p.work_item_id, "claimed")
    update_pack_status(tmp_path, p.work_item_id, "resolved", LINT)
    (tmp_path / tm.PRS_CACHE_NAME).write_text(json.dumps({"prs": [PR]}))
    mirrors = tmp_path / "mirrors.yaml"
    mirrors.write_text("mirrors: []\n")

    r1 = tm.refresh(tmp_path, mirrors_path=mirrors)
    r2 = tm.refresh(tmp_path, mirrors_path=mirrors)
    assert r1["headline"]["false_resolve_rate"] == 1.0
    assert r1["agent_prs"]["merged"] == 1 and r1["agent_prs"]["observed"] == 0
    assert tm.load_report(tmp_path)["headline"]["at"] == r2["headline"]["at"]
    assert len(tm.load_history(tmp_path)) == 2


def test_emit_links_a_comeback_to_its_resolved_pack(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")

    def f(title: str, line: int = 10) -> Finding:
        return Finding(id=f"llm:u:{line}", type="bug", severity="HIGH", repo="x", file_path="/m/x/s3.py", line=line,
                       title=title, description="d", evidence="e", recommendation="r", source="llm")

    [fixed] = emit_packs_from_findings([f("Pickle loaded with weights_only=False")], output_dir=tmp_path,
                                       kind="nightly", run_id="run-1")
    update_pack_status(tmp_path, fixed.work_item_id, "resolved", PYTEST)
    [back] = emit_packs_from_findings([f("Pickle loaded with weights_only=False here too", 12)],
                                      output_dir=tmp_path, kind="nightly", run_id="run-2")
    assert back.work_item_id != fixed.work_item_id
    data = load_pack(tmp_path, back.work_item_id)
    assert data["status"] == "open" and data["governance"]["recurrence_of"] == fixed.work_item_id
    # a re-emit of the comeback keeps the link
    [again] = emit_packs_from_findings([f("Pickle loaded with weights_only=False here too", 12)],
                                       output_dir=tmp_path, kind="nightly", run_id="run-2")
    assert load_pack(tmp_path, again.work_item_id)["governance"]["recurrence_of"] == fixed.work_item_id
    r = tm.recurrence_metric(tm.load_packs(tmp_path), [])
    assert r["regressed"] == 1 and "recurrence_of" in r["examples"][0]["evidence"]


# ── API, page, pipeline tail ─────────────────────────────────────────────────

def test_api_serves_report_history_and_page(tmp_path: Path, memdb, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from auditor.audit_api import app

    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(tmp_path))
    client = TestClient(app)
    assert client.get("/audit/trust.json").status_code == 404
    assert client.get("/audit/trust.json?history=1").json() == {"history": []}
    mirrors = tmp_path / "mirrors.yaml"
    mirrors.write_text("mirrors: []\n")
    tm.refresh(tmp_path, mirrors_path=mirrors)
    body = client.get("/audit/trust.json").json()
    assert set(body) >= {"false_resolve", "reopen", "recurrence", "scan_persistence", "agent_prs", "flow", "headline"}
    assert body["agent_prs"] is None  # no PR list cached: unknown, not zero
    assert len(client.get("/audit/trust.json?history=1").json()["history"]) == 1
    page = client.get("/ui/trust")
    assert page.status_code == 200 and 'aria-current="page">Trust</a>' in page.text
    assert "/audit/trust.json" in page.text


def test_refresh_endpoint_requires_auth(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from auditor.audit_api import app

    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "secret")
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(tmp_path))
    assert TestClient(app).post("/audit/trust/refresh").status_code in (401, 403)


def test_refresh_quietly_respects_toggle_and_swallows_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_TRUST_METRICS", "0")
    assert "skipped" in tm.refresh_quietly(tmp_path)
    monkeypatch.setenv("AI_AUDIT_TRUST_METRICS", "1")
    monkeypatch.setattr(tm, "refresh", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert tm.refresh_quietly(tmp_path) == {"error": "boom"}
