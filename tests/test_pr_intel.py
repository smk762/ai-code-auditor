from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import pr_intel
from auditor.pr_intel import (
    PrIntel,
    build_report,
    classify,
    fetch_repo_prs,
    parse_dependencies,
    resolution_plan,
    spoken_summary,
)
from conductor.briefing import build_briefing
from conductor.publish import md_passes_tts_rules
from conductor.schemas import FindingsSummary, MithrandirStatus, RestWindowInfo
from conductor.wrapup import _cached_pr_brief

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def _pr(repo: str, number: int, **kw) -> PrIntel:
    base = dict(
        repo=repo, slug=f"smk762/{repo}", number=number, title=f"PR {number}",
        url=f"https://github.com/smk762/{repo}/pull/{number}", author="smk762",
        base="dev", head=f"feat-{number}", default_branch="dev", draft=False,
        created_at=(NOW - timedelta(days=2)).isoformat(), updated_at=(NOW - timedelta(days=1)).isoformat(),
        mergeable="MERGEABLE", merge_state="CLEAN", review_decision="",
    )
    base.update(kw)
    return PrIntel(**base)


def _kinds(p: PrIntel) -> list[str]:
    return [b.kind for b in p.blockers]


# ── dependency parsing ─────────────────────────────────────────────────────────


def test_parse_dependencies_ref_forms():
    body = (
        "Stacked on #24, which adds this file.\n"
        "Depends on https://github.com/smk762/argus-ci/pull/2 (move v1 after that merges).\n"
        "Merge after argus-mint#16. Blocked by other/lib#7.\n"
        "Part of #31. See #40."
    )
    deps = parse_dependencies(body, owner="smk762", repo="argus-halo")
    assert deps == [
        {"slug": "smk762/argus-halo", "number": 24, "verb": "stacked"},
        {"slug": "smk762/argus-ci", "number": 2, "verb": "depends"},
        {"slug": "smk762/argus-mint", "number": 16, "verb": "depends"},
        {"slug": "other/lib", "number": 7, "verb": "depends"},
    ]


def test_parse_dependencies_ignores_mentions():
    assert parse_dependencies("Part of #31; closes #12; see argus-mask#57.", owner="o", repo="r") == []


# ── classification ────────────────────────────────────────────────────────────


def test_classify_stacked_by_base_branch_and_transitive_blocks():
    parent = _pr("argus-halo", 24, head="docs/claude-md")
    child = _pr("argus-halo", 26, base="docs/claude-md", head="docs/suite")
    grandchild = _pr("argus-halo", 27, base="docs/suite")
    classify([parent, child, grandchild], now=NOW)
    assert _kinds(child) == ["stacked"] and child.state == "waiting"
    assert child.next_action == "Wait for argus-halo#24; then merge"
    assert parent.state == "ready" and parent.blocks == ["argus-halo#26", "argus-halo#27"]
    assert parent.score > child.score


def test_classify_conflict_ci_and_ready():
    dirty = _pr("argus-assay", 10, mergeable="CONFLICTING", merge_state="DIRTY")
    red = _pr("argus-mask", 76, merge_state="UNSTABLE",
              failed_checks=[{"name": "ci / test (3.11)", "url": "https://github.com/x/y/actions/runs/1"}])
    ok = _pr("argus-ci", 9, review_decision="REVIEW_REQUIRED")
    classify([dirty, red, ok], now=NOW)
    assert dirty.state == "blocked" and dirty.next_action == "Merge dev into feat-10 and resolve conflicts"
    assert red.state == "blocked" and red.next_action == "Fix failing CI (ci / test (3.11))"
    assert red.blockers[0].url.endswith("/runs/1")
    # sole maintainer: review_required alone still counts as ready
    assert ok.state == "ready" and ok.next_action == "Review and merge"


def test_classify_base_rules():
    release = _pr("argus-mask", 76, title="Release 0.1.0", base="main", head="dev")
    stray = _pr("argus-mint", 5, base="main")
    orphan = _pr("argus-mint", 6, base="feature/gone")
    classify([release, stray, orphan], now=NOW)
    assert release.release and release.blockers == [] and release.state == "ready"
    assert _kinds(stray) == ["wrong_base"] and stray.next_action == "Retarget to dev"
    assert _kinds(orphan) == ["wrong_base"]


def test_classify_external_dependency_resolution():
    p = _pr("argus-pkg-template", 7, deps=[
        {"slug": "smk762/argus-ci", "number": 2, "verb": "depends"},
        {"slug": "smk762/argus-ci", "number": 3, "verb": "depends"},
    ])
    q = _pr("argus-mint", 17, deps=[{"slug": "smk762/argus-mint", "number": 16, "verb": "stacked"}])
    states = {2: "OPEN", 3: "MERGED", 16: "CLOSED"}

    def resolve(slug, n):
        return {"state": states[n], "title": f"t{n}", "url": f"https://github.com/{slug}/pull/{n}"}

    classify([p, q], resolve_ref=resolve, now=NOW)
    assert [(b.kind, b.ref) for b in p.blockers] == [("depends_on", "argus-ci#2")]
    assert p.state == "waiting"
    assert _kinds(q) == ["dep_closed"] and q.state == "blocked"
    assert q.next_action.startswith("Retarget to dev")


def test_draft_is_parked_not_blocked():
    d = _pr("argus-mint", 17, draft=True, mergeable="CONFLICTING", merge_state="DIRTY")
    classify([d], now=NOW)
    assert d.state == "draft" and "conflict" in _kinds(d)
    assert resolution_plan([d]) == []


# ── plan ──────────────────────────────────────────────────────────────────────


def test_plan_puts_prerequisites_first_even_across_repos():
    dep = _pr("argus-mint", 30, updated_at=NOW.isoformat())  # fresh, low score on its own
    big = _pr("argus-assay", 10, mergeable="CONFLICTING", merge_state="DIRTY",
              deps=[{"slug": "smk762/argus-mint", "number": 30, "verb": "depends"}],
              updated_at=(NOW - timedelta(days=30)).isoformat())
    rel = _pr("argus-survey", 45, title="Release 0.1.0", base="main", head="dev")
    classify([dep, big, rel], now=NOW)
    plan = resolution_plan([big, rel, dep])
    order = [s["ref"] for s in plan]
    assert order.index("argus-mint#30") < order.index("argus-assay#10")
    step = plan[order.index("argus-assay#10")]
    assert step["after"] == ["argus-mint#30"]
    assert step["action"].startswith("Resolve conflicts after argus-mint#30")
    assert plan[order.index("argus-mint#30")]["unblocks"] == ["argus-assay#10"]


# ── fetching ──────────────────────────────────────────────────────────────────


def _node(number: int, mergeable: str, **kw):
    return {
        "number": number, "title": f"PR {number}", "url": f"https://github.com/smk762/r/pull/{number}",
        "isDraft": False, "createdAt": NOW.isoformat(), "updatedAt": NOW.isoformat(),
        "baseRefName": "dev", "headRefName": f"h{number}", "body": kw.get("body", ""),
        "mergeable": mergeable, "mergeStateStatus": "CLEAN" if mergeable == "MERGEABLE" else mergeable,
        "reviewDecision": None, "author": {"login": "smk762"}, "labels": {"nodes": []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "lint", "conclusion": "FAILURE", "status": "COMPLETED", "detailsUrl": "u"},
            {"__typename": "CheckRun", "name": "test", "conclusion": None, "status": "IN_PROGRESS", "detailsUrl": ""},
            {"__typename": "StatusContext", "context": "ext", "state": "SUCCESS", "targetUrl": ""},
        ]}}}}]},
    }


def test_fetch_retries_while_mergeable_unknown():
    calls = []

    def gql(query, variables):
        calls.append(variables)
        m = "UNKNOWN" if len(calls) == 1 else "CONFLICTING"
        return {"repository": {"defaultBranchRef": {"name": "dev"}, "pullRequests": {"nodes": [_node(1, m)]}}}

    prs = fetch_repo_prs(gql, "r", "smk762/r", retry_wait_s=0)
    assert len(calls) == 2
    assert prs[0].mergeable == "CONFLICTING"
    assert prs[0].failed_checks == [{"name": "lint", "url": "u"}] and prs[0].pending_checks == 1


def test_build_report_records_repo_errors_and_counts():
    def gql(query, variables):
        if variables.get("name") == "broken":
            raise RuntimeError("Could not resolve to a Repository")
        if "issueOrPullRequest" in query:
            return {"repository": {"issueOrPullRequest": {"state": "OPEN", "title": "x", "url": ""}}}
        return {"repository": {"defaultBranchRef": {"name": "dev"}, "pullRequests": {"nodes": [
            _node(1, "MERGEABLE", body="Depends on smk762/other#9."),
        ]}}}

    report = build_report([("r", "smk762/r"), ("broken", "smk762/broken")], gql, now=NOW, retry_wait_s=0)
    assert report["errors"][0]["repo"] == "broken"
    assert report["counts"]["open"] == 1
    p = report["prs"][0]
    assert p["state"] == "blocked"  # failing lint beats the open dependency
    assert {b["kind"] for b in p["blockers"]} == {"depends_on", "ci_failing"}
    json.dumps(report)  # serialisable


# ── spoken summary + briefing ─────────────────────────────────────────────────


def _report(prs: list[PrIntel]) -> dict:
    classify(prs, now=NOW)
    counts = {"open": len(prs)}
    for s in ("blocked", "waiting", "pending", "ready", "draft"):
        counts[s] = sum(1 for p in prs if p.state == s)
    prs.sort(key=lambda p: (p.state != "blocked", -p.score))
    rep = {"counts": counts, "prs": [p.to_dict(NOW) for p in prs], "plan": resolution_plan(prs)}
    rep["spoken"] = spoken_summary(rep)
    return rep


def test_spoken_summary_is_tts_safe_and_names_top_blockers():
    rep = _report([
        _pr("argus-mask", 76, title="Release 0.1.0", base="main", head="dev",
            failed_checks=[{"name": "ci / test (3.11)", "url": "https://github.com/x"}]),
        _pr("argus-assay", 10, mergeable="CONFLICTING", merge_state="DIRTY"),
        _pr("argus-ci", 9),
    ])
    s = rep["spoken"]
    assert s.startswith("Pull requests: 3 open, 2 blocked, 0 waiting on another, 1 ready to merge.")
    assert "argus-mask#76: fix failing CI, it holds a release" in s
    assert "argus-assay#10: resolve merge conflicts" in s
    assert md_passes_tts_rules(s)[0]


def test_spoken_summary_nothing_stuck_and_empty():
    rep = _report([_pr("argus-ci", 9)])
    assert rep["spoken"].endswith("Nothing is stuck; merge argus-ci#9 first.")
    assert spoken_summary({"counts": {"open": 0}}) == ""


def _status(**kw) -> MithrandirStatus:
    return MithrandirStatus(
        run_id="t", generated_at=NOW.isoformat(), ended_reason="completed", summary_one_liner="x",
        speakable=True, latest_url="u", latest_md_url="u", window=RestWindowInfo(start="00:30", end="06:00"),
        findings=FindingsSummary(), next_rest_window="2026-11-15 00:30 AWST", **kw,
    )


def test_briefing_speaks_pr_brief_before_next_window():
    brief = "Pull requests: 3 open, 1 blocked. Start with argus-assay#10: resolve merge conflicts."
    md, ok = build_briefing(_status(pr_brief=brief))
    assert ok
    assert brief in md and md.endswith("Next rest window starts 2026-11-15 00:30 AWST.")
    assert _status(pr_brief=brief).to_dict()["pr_brief"] == brief
    assert "pr_brief" not in _status().to_dict()


def test_briefing_drops_whole_pr_sentences_over_word_limit():
    long_brief = "Pull requests: 3 open. " + " ".join(["Start with argus-x#1: fix it."] * 60)
    md, ok = build_briefing(_status(pr_brief=long_brief))
    assert ok and len(md.split()) <= 150
    assert "Pull requests: 3 open." in md and md.endswith("AWST.")


def test_wrapup_uses_only_a_fresh_cache(tmp_path: Path):
    path = tmp_path / "pr_intel.json"
    assert _cached_pr_brief(tmp_path) == ""
    path.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "spoken": "fresh"}))
    assert _cached_pr_brief(tmp_path) == "fresh"
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    path.write_text(json.dumps({"generated_at": old, "spoken": "stale"}))
    assert _cached_pr_brief(tmp_path) == ""


def test_wrapup_cache_respects_kill_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "pr_intel.json").write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "spoken": "fresh"})
    )
    monkeypatch.setenv("AI_AUDIT_PR_INTEL", "0")
    assert _cached_pr_brief(tmp_path) == ""


# ── API ───────────────────────────────────────────────────────────────────────


def test_api_dashboard_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from auditor.audit_api import app

    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(tmp_path))
    client = TestClient(app)
    assert client.get("/audit/prs.json").status_code == 404
    page = client.get("/audit/prs")
    assert page.status_code == 200 and "Suggested resolution path" in page.text
    (tmp_path / pr_intel.REPORT_NAME).write_text(json.dumps({"counts": {"open": 0}, "prs": [], "plan": []}))
    r = client.get("/audit/prs.json")
    assert r.status_code == 200 and r.json()["counts"] == {"open": 0}
