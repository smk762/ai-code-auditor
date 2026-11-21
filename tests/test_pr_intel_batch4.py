"""pr_intel: fork heads are never stack parents, prose "after #N" is no dependency, and the
plan only says "Merge" once every wait is cleared by an earlier step."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auditor.pr_intel import Blocker, PrIntel, _node_to_intel, classify, parse_dependencies, resolution_plan

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


def test_fork_head_is_never_a_stack_parent():
    fork = _pr("argus-halo", 5, head="dev", cross_repo=True)
    mine = _pr("argus-halo", 6, base="dev")
    classify([fork, mine], now=NOW)
    assert [b.kind for b in mine.blockers] == []
    assert mine.state == "ready" and fork.blocks == []


def test_fork_head_named_like_a_stray_base_still_gets_the_wrong_base_check():
    fork = _pr("argus-halo", 5, head="feature-x", cross_repo=True)
    mine = _pr("argus-halo", 6, base="feature-x")
    classify([fork, mine], now=NOW)
    assert [b.kind for b in mine.blockers] == ["wrong_base"]


def test_same_repo_head_is_still_a_stack_parent():
    parent = _pr("argus-halo", 5, head="topic")
    child = _pr("argus-halo", 6, base="topic")
    classify([parent, child], now=NOW)
    assert [b.kind for b in child.blockers] == ["stacked"]


def test_node_reads_is_cross_repository():
    n = {"number": 1, "title": "t", "headRefName": "main", "baseRefName": "dev", "isCrossRepository": True}
    assert _node_to_intel("r", "o/r", "dev", n).cross_repo is True
    assert _node_to_intel("r", "o/r", "dev", {**n, "isCrossRepository": False}).cross_repo is False


@pytest.mark.parametrize("body", [
    "Regression introduced after #120.",
    "This broke after owner/repo#5 landed.",
    "thereafter #7",
    "Fixes the crash seen right after argus-mint#3",
    "underrequires #4",
])
def test_prose_after_is_not_a_dependency(body: str):
    assert parse_dependencies(body, owner="smk762", repo="argus-halo") == []


@pytest.mark.parametrize("body", ["Merge after #9.", "Lands after #9", "land after #9", "Depends on #9"])
def test_explicit_dependency_verbs_still_parse(body: str):
    deps = parse_dependencies(body, owner="smk762", repo="argus-halo")
    assert [(d["slug"], d["number"]) for d in deps] == [("smk762/argus-halo", 9)]


def _steps(prs: list[PrIntel]) -> dict[str, str]:
    return {s["ref"]: s["action"] for s in resolution_plan(prs)}


def test_plan_keeps_wait_while_an_external_wait_is_open():
    dep = _pr("argus-halo", 1)
    child = _pr("argus-halo", 2, deps=[{"slug": "smk762/argus-halo", "number": 1, "verb": "depends"},
                                       {"slug": "other/lib", "number": 7, "verb": "depends"}])
    classify([dep, child], now=NOW,
             resolve_ref=lambda slug, n: {"state": "OPEN", "title": "lib", "url": "u"})
    actions = _steps([dep, child])
    assert actions["argus-halo#2"].startswith("Wait for")
    assert "lib#7" in actions["argus-halo#2"]


def test_plan_says_merge_once_in_plan_waits_are_earlier_steps():
    dep = _pr("argus-halo", 1)
    child = _pr("argus-halo", 2, deps=[{"slug": "smk762/argus-halo", "number": 1, "verb": "depends"}])
    classify([dep, child], now=NOW)
    assert _steps([dep, child])["argus-halo#2"] == "Merge"


def test_plan_does_not_say_merge_on_a_cycle_break():
    a = _pr("argus-halo", 1, deps=[{"slug": "smk762/argus-halo", "number": 2, "verb": "depends"}])
    b = _pr("argus-halo", 2, deps=[{"slug": "smk762/argus-halo", "number": 1, "verb": "depends"}])
    classify([a, b], now=NOW)
    steps = resolution_plan([a, b])
    assert steps[0]["action"].startswith("Break cycle: Wait for")
    assert steps[1]["action"] == "Merge"  # its one wait is the step before it


def test_plan_keeps_wait_for_a_refless_wait():
    dep = _pr("argus-halo", 1)
    child = _pr("argus-halo", 2, deps=[{"slug": "smk762/argus-halo", "number": 1, "verb": "depends"}])
    classify([dep, child], now=NOW)
    child.blockers.append(Blocker("depends_on", "waits on something unnamed"))
    assert _steps([dep, child])["argus-halo#2"].startswith("Wait for")
