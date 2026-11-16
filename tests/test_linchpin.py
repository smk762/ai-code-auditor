from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auditor.linchpin import (
    IssueIntel,
    analyse,
    apply_labels,
    issue_from_node,
    spoken_linchpins,
)
from auditor.pr_intel import (
    PrIntel,
    build_report,
    classify,
    parse_dependants,
    resolution_plan,
)
from conductor.publish import md_passes_tts_rules

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


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


def _dep(repo: str, n: int, verb: str = "depends") -> dict:
    return {"slug": f"smk762/{repo}", "number": n, "verb": verb}


def _issue(repo: str, number: int, **kw) -> IssueIntel:
    return IssueIntel(repo=repo, slug=f"smk762/{repo}", number=number, title=f"Issue {number}",
                      url=f"https://github.com/smk762/{repo}/issues/{number}", **kw)


def _scan(prs, issues=(), **kw):
    classify(prs, now=NOW, resolve_ref=kw.get("resolve_ref"))
    return analyse(prs, list(issues), now=NOW, **kw)


def _refs(scan) -> list[str]:
    return [lp["ref"] for lp in scan["linchpins"]]


def test_parse_dependants_reverse_verbs():
    body = "Blocks argus-lens#96. Unblocks https://github.com/smk762/argus-mint/issues/4. Code blocks are fine."
    assert parse_dependants(body, owner="smk762", repo="argus-curator") == [
        {"slug": "smk762/argus-lens", "number": 96},
        {"slug": "smk762/argus-mint", "number": 4},
    ]
    assert parse_dependants("Part of #3; closes #4; non-blocking #5", owner="o", repo="r") == []


def test_cross_repo_chain_marks_only_the_root():
    root = _pr("argus-curator", 160, head="cortex-0.6")
    mid = _pr("argus-curator", 157, base="cortex-0.6")  # stacked on 160
    leaf = _pr("argus-darkroom", 56, deps=[_dep("argus-curator", 157)])
    local = _pr("argus-mint", 3, deps=[_dep("argus-mint", 2)])
    local_root = _pr("argus-mint", 2)
    scan = _scan([root, mid, leaf, local, local_root])
    assert _refs(scan) == ["argus-curator#160"]
    lp = scan["linchpins"][0]
    assert lp["reasons"] == ["cross_repo"] and lp["repos_held"] == ["argus-darkroom"]
    assert lp["chains"] == [["argus-curator#160", "argus-curator#157", "argus-darkroom#56"]]
    assert root.p0 and not mid.p0 and not local_root.p0  # same-repo chains are not P0
    plan = resolution_plan([root, mid, leaf, local, local_root])
    assert plan[0]["ref"] == "argus-curator#160" and plan[0]["p0"]


def test_chain_through_issues_and_closing_pr():
    # argus-studio#126 (issue) depends on argus-lens#96 (issue), which argus-lens#97 closes.
    epic = _issue("argus-studio", 126, deps=[_dep("argus-lens", 96)])
    bug = _issue("argus-lens", 96)
    fix = _pr("argus-lens", 97, closes=[{"slug": "smk762/argus-lens", "number": 96}])
    scan = _scan([fix], [epic, bug])
    assert _refs(scan) == ["argus-lens#97"]
    assert scan["linchpins"][0]["chains"] == [["argus-lens#97", "argus-lens#96", "argus-studio#126"]]
    assert {i["ref"] for i in scan["issues"]} == {"argus-studio#126", "argus-lens#96"}


def test_open_issue_with_no_fix_is_itself_the_linchpin():
    scan = _scan([], [_issue("argus-curator", 113, deps=[_dep("argus-quarry", 19)]), _issue("argus-quarry", 19)])
    lp = scan["linchpins"][0]
    assert lp["ref"] == "argus-quarry#19" and lp["kind"] == "issue"
    assert lp["next_action"].startswith("Resolve it")


def test_cycle_back_to_origin_repo_marks_every_member_and_suggests_soft_cut():
    a = _pr("argus-cortex", 90, head="cortex-x")
    b = _pr("argus-cortex", 91, base="cortex-x", deps=[])  # stacked on a (hard edge)
    c = _pr("argus-mint", 20, deps=[_dep("argus-cortex", 91)])
    a.deps = [_dep("argus-mint", 20)]  # a "depends on" c → cycle back to argus-cortex
    outside = _pr("argus-lens", 5, deps=[_dep("argus-mint", 20)])
    scan = _scan([a, b, c, outside])
    assert len(scan["cycles"]) == 1
    cy = scan["cycles"][0]
    assert cy["members"] == ["argus-cortex#90", "argus-cortex#91", "argus-mint#20"]
    assert cy["cross_repo"] and cy["loop"][0] == cy["loop"][-1] == "argus-cortex#90"
    # the stacked edge (91 → 90) is structural; cut a text claim instead
    assert (cy["cut"]["from"], cy["cut"]["to"], cy["cut"]["source"]) == ("argus-cortex#90", "argus-mint#20", "body")
    assert set(_refs(scan)) == set(cy["members"])
    assert all(p.p0 for p in (a, b, c)) and not outside.p0
    assert all(lp["next_action"].startswith("Break the cycle") for lp in scan["linchpins"])
    s = spoken_linchpins({"cycles": scan["cycles"], "linchpins": scan["linchpins"]})
    assert s.startswith("Dependency cycle: argus-cortex#90, argus-cortex#91 and argus-mint#20 wait on each other.")
    assert md_passes_tts_rules(s)[0]


def test_resolution_plan_still_terminates_on_cycle():
    a = _pr("argus-a", 1, deps=[_dep("argus-b", 1)])
    b = _pr("argus-b", 1, deps=[_dep("argus-a", 1)])
    _scan([a, b])
    assert {s["ref"] for s in resolution_plan([a, b])} == {"argus-a#1", "argus-b#1"}


def test_native_links_and_external_refs():
    # Native: argus-atlas#7 blocked by argus-ci#2 (open, not mirrored); closed ones are done.
    node = {
        "number": 7, "title": "t", "url": "u", "body": "",
        "labels": {"nodes": [{"name": "P0"}]},
        "blockedBy": {"nodes": [
            {"number": 2, "title": "ci", "url": "https://github.com/other/argus-ci/issues/2", "state": "OPEN",
             "repository": {"nameWithOwner": "other/argus-ci"}},
            {"number": 3, "title": "old", "url": "", "state": "CLOSED", "repository": {"nameWithOwner": "smk762/argus-mint"}},
        ]},
        "blocking": {"nodes": []},
    }
    issue = issue_from_node("argus-atlas", "smk762/argus-atlas", node)
    assert issue.labels == ["P0"] and len(issue.blocked_by) == 2
    scan = _scan([], [issue], mirrored={"smk762/argus-atlas"})
    assert scan["edges"] == [{"from": "argus-atlas#7", "to": "argus-ci#2", "source": "native"}]
    lp = scan["linchpins"][0]
    assert lp["ref"] == "argus-ci#2" and lp["external"]


def test_unresolvable_or_merged_deps_add_no_edge():
    p = _pr("argus-x", 1, deps=[_dep("argus-y", 9)])
    scan = _scan([p], resolve_ref=lambda slug, n: {"state": "MERGED", "title": "", "url": ""})
    assert scan["edges"] == [] and scan["linchpins"] == []


# ── labels ────────────────────────────────────────────────────────────────────


def _lp(repo: str, n: int, **kw) -> dict:
    return {"ref": f"{repo}#{n}", "slug": f"smk762/{repo}", "number": n, "labels": [], "external": False, **kw}


def test_apply_labels_adds_removes_only_its_own_and_records_ledger(tmp_path: Path):
    calls: list[tuple[str, str]] = []

    def rest(method, path, body=None):
        calls.append((method, path))

    ledger = tmp_path / "linchpin_labels.json"
    ledger.write_text(json.dumps({"label": "P0", "applied": {"smk762/argus-old#4": "x"}}))
    report = {"linchpins": [
        _lp("argus-curator", 157),
        _lp("argus-mint", 2, labels=["P0"]),  # labelled by a human: leave alone
        _lp("argus-ci", 2, external=True),  # not mirrored: never labelled
    ]}
    out = apply_labels(report, output_dir=tmp_path, rest=rest)
    assert out["added"] == ["argus-curator#157"] and out["removed"] == ["argus-old#4"] and not out["errors"]
    assert calls == [
        ("POST", "/repos/smk762/argus-curator/issues/157/labels"),
        ("DELETE", "/repos/smk762/argus-old/issues/4/labels/P0"),
    ]
    assert set(json.loads(ledger.read_text())["applied"]) == {"smk762/argus-curator#157"}
    # second run: already applied, nothing to do; human-labelled one never enters the ledger
    calls.clear()
    out = apply_labels(report, output_dir=tmp_path, rest=rest)
    assert calls == [] and out["added"] == [] and out["removed"] == []


def test_apply_labels_dry_run_writes_nothing_and_404_counts_as_removed(tmp_path: Path):
    def boom(method, path, body=None):
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    out = apply_labels({"linchpins": [_lp("argus-a", 1)]}, output_dir=tmp_path, dry_run=True, rest=boom)
    assert out["added"] == ["argus-a#1"] and not (tmp_path / "linchpin_labels.json").exists()
    (tmp_path / "linchpin_labels.json").write_text(json.dumps({"label": "P0", "applied": {"smk762/argus-b#2": "x"}}))
    out = apply_labels({"linchpins": []}, output_dir=tmp_path, rest=boom)
    assert out["removed"] == ["argus-b#2"]
    assert json.loads((tmp_path / "linchpin_labels.json").read_text())["applied"] == {}


def test_apply_labels_records_errors_and_keeps_going(tmp_path: Path):
    def rest(method, path, body=None):
        if "argus-a" in path:
            raise urllib.error.HTTPError(path, 403, "Resource not accessible", {}, None)

    out = apply_labels({"linchpins": [_lp("argus-a", 1), _lp("argus-b", 2)]}, output_dir=tmp_path, rest=rest)
    assert out["added"] == ["argus-b#2"] and out["errors"][0]["ref"] == "argus-a#1"


# ── report wiring ─────────────────────────────────────────────────────────────


def _pr_node(number: int, **kw) -> dict:
    return {
        "number": number, "title": f"PR {number}", "url": f"https://github.com/smk762/r/pull/{number}",
        "isDraft": False, "createdAt": NOW.isoformat(), "updatedAt": NOW.isoformat(),
        "baseRefName": "dev", "headRefName": f"h{number}", "body": kw.get("body", ""),
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": None,
        "author": {"login": "smk762"}, "labels": {"nodes": []},
        "closingIssuesReferences": {"nodes": kw.get("closes", [])}, "commits": {"nodes": []},
    }


def test_build_report_includes_issues_linchpins_and_spoken(monkeypatch: pytest.MonkeyPatch):
    data = {
        "argus-lens": {"pullRequests": [_pr_node(97, body="Blocks argus-studio#126.")], "issues": []},
        "argus-studio": {"pullRequests": [], "issues": [
            {"number": 126, "title": "Epic", "url": "https://github.com/smk762/argus-studio/issues/126", "body": "",
             "labels": {"nodes": []}, "blockedBy": {"nodes": []}, "blocking": {"nodes": []}},
        ]},
    }

    def gql(query, variables):
        d = data[variables["name"]]
        return {"repository": {"defaultBranchRef": {"name": "dev"},
                               "pullRequests": {"nodes": d["pullRequests"]}, "issues": {"nodes": d["issues"]}}}

    report = build_report([("argus-lens", "smk762/argus-lens"), ("argus-studio", "smk762/argus-studio")],
                          gql, now=NOW, retry_wait_s=0)
    assert report["counts"]["p0"] == 1 and report["counts"]["cycles"] == 0
    assert report["linchpins"][0]["ref"] == "argus-lens#97"
    assert report["prs"][0]["p0"] and report["prs"][0]["linchpin"] == ["cross_repo"]
    assert "P0 linchpin: argus-lens#97 holds up argus-studio." in report["spoken"]
    json.dumps(report)
