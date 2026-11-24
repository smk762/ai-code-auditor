"""Architecture packs: one pack per issue across runs, closed when a run stops reporting it."""
from __future__ import annotations

from pathlib import Path

import pytest

from auditor import pack_gc
from auditor.agent_pack import (
    architecture_findings,
    emit_packs_from_findings,
    emit_packs_from_violations,
    finding_from_violation,
    load_pack,
    update_pack_status,
)
from auditor.contracts import RuleViolation


def call(frm: str = "argus-assay", to: str = "argus-darkroom", line: int = 451) -> RuleViolation:
    return RuleViolation(rule_name="call_graph_violation", violating_path=f"{frm} → {to}", entities=[frm, to],
                         evidence=f"Unauthorised direct call. Locations: record_demo.py:{line} (httpx.get())")


def ui(repo: str = "agent-composer", path: str = "rag-ingest/app/main.py", line: int = 484) -> RuleViolation:
    return RuleViolation(rule_name="ui_in_worker", violating_path=f"{repo}: {path}:{line}", entities=[repo],
                         evidence=f"HTML response from a worker at {path}:{line}")


def emit(out: Path, violations: list[RuleViolation], run: str, scanned: set[str] | None = None,
         **kw) -> dict:
    emit_packs_from_violations(violations, output_dir=out, run_id=run)
    kw.setdefault("unread", {})
    repos = scanned if scanned is not None else {"argus-assay", "agent-composer", "argus-survey"}
    return pack_gc.close_unreported_architecture(out, architecture_findings(violations), repos, **kw)


def test_a_violation_keeps_one_pack_across_runs(tmp_path: Path) -> None:
    [a] = emit_packs_from_violations([call(line=451)], output_dir=tmp_path, run_id="run-1")
    update_pack_status(tmp_path, a.work_item_id, "claimed", "agent-x")
    [b] = emit_packs_from_violations([call(line=456)], output_dir=tmp_path, run_id="run-2")  # code moved
    assert a.work_item_id == b.work_item_id and "-arch-" in a.work_item_id
    assert load_pack(tmp_path, a.work_item_id)["status"] == "claimed"
    assert len(list((tmp_path / "agent_packs").glob("pack-*.yaml"))) == 1


def test_one_rule_in_one_file_at_several_lines_is_one_pack(tmp_path: Path) -> None:
    packs = emit_packs_from_violations([ui(line=484), ui(line=515), ui(path="rag-chat/app/routers/ui.py")],
                                       output_dir=tmp_path, run_id="run-1")
    assert len(packs) == 2
    brief = (tmp_path / "agent_packs" / f"{packs[0].work_item_id}.md").read_text()
    assert "main.py:484" in brief and "main.py:515" in brief


def test_an_unreported_pack_closes_and_a_comeback_reopens_it(tmp_path: Path) -> None:
    emit(tmp_path, [call(), ui()], "run-1")
    darkroom = architecture_findings([call()])[0].id
    rep = emit(tmp_path, [ui()], "run-2")
    assert rep["closed"] == 1 and rep["by_rule"] == {"not_reported": 1}
    data = load_pack(tmp_path, darkroom)
    assert data["status"] == "obsolete" and data["governance"]["gc"]["rule"] == "not_reported"
    assert (tmp_path / pack_gc.ARCH_REPORT_NAME).exists()
    assert emit(tmp_path, [ui()], "run-3")["planned"] == 0  # idempotent
    emit(tmp_path, [call(), ui()], "run-4")
    assert load_pack(tmp_path, darkroom)["status"] == "open"


@pytest.mark.parametrize("status", ["claimed", "in_progress", "unverified", "wontfix", "resolved"])
def test_only_open_architecture_packs_are_closed(tmp_path: Path, status: str) -> None:
    emit(tmp_path, [call()], "run-1")
    pid = architecture_findings([call()])[0].id
    update_pack_status(tmp_path, pid, status, "someone is on it" if status != "resolved" else "fixed")
    assert emit(tmp_path, [], "run-2")["planned"] == 0
    assert load_pack(tmp_path, pid)["status"] == status


def test_a_pack_claimed_after_planning_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    emit(tmp_path, [call()], "run-1")
    pid = architecture_findings([call()])[0].id
    real_plan = pack_gc.plan_architecture

    def plan_then_claim(*a, **kw):
        p = real_plan(*a, **kw)
        update_pack_status(tmp_path, pid, "claimed", "agent-x")
        return p
    monkeypatch.setattr(pack_gc, "plan_architecture", plan_then_claim)
    assert emit(tmp_path, [], "run-2")["closed"] == 0
    assert load_pack(tmp_path, pid)["status"] == "claimed"


def test_a_repo_the_run_did_not_scan_is_left_alone(tmp_path: Path) -> None:
    emit(tmp_path, [call()], "run-1")
    assert emit(tmp_path, [], "run-2", scanned={"agent-composer"})["planned"] == 0


def test_packs_from_before_stable_ids_close_as_duplicates(tmp_path: Path) -> None:
    # The old emitter keyed packs on the run, so each night added a twin.
    for run in ("old-1", "old-2"):
        emit_packs_from_findings([finding_from_violation(call())], output_dir=tmp_path, kind="architecture",
                                 run_id=run)
    emit_packs_from_findings([finding_from_violation(ui(line=487))], output_dir=tmp_path, kind="architecture",
                             run_id="old-1")
    rep = emit(tmp_path, [call(), ui(line=484)], "run-1")
    assert rep["by_rule"] == {"duplicate": 3} and not rep["held"]
    keepers = {f.id for f in architecture_findings([call(), ui()])}
    for c in rep["closures"]:
        data = load_pack(tmp_path, c["pack_id"])
        assert data["status"] == "wontfix" and data["governance"]["duplicate_of"] in keepers
    assert all(load_pack(tmp_path, k)["status"] == "open" for k in keepers)


def test_mass_architecture_closure_is_held_unless_forced(tmp_path: Path) -> None:
    many = [ui(path=f"svc/m{i}.py") for i in range(pack_gc.HOLD_MIN + 2)]
    emit(tmp_path, many, "run-1")
    rep = emit(tmp_path, [], "run-2")  # an empty run looks like everything was fixed
    assert rep["planned"] == 0 and rep["closed"] == 0 and "rerun with force" in rep["held"]
    assert emit(tmp_path, [], "run-3", force=True)["closed"] == len(many)


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    emit(tmp_path, [call()], "run-1")
    (tmp_path / pack_gc.ARCH_REPORT_NAME).unlink()
    rep = emit(tmp_path, [], "run-2", dry_run=True)
    assert rep["planned"] == 1 and rep["closed"] == 0
    assert load_pack(tmp_path, architecture_findings([call()])[0].id)["status"] == "open"
    assert not (tmp_path / pack_gc.ARCH_REPORT_NAME).exists()


@pytest.mark.parametrize("force", [False, True])
def test_a_repo_with_an_unread_file_closes_nothing_as_not_reported(tmp_path: Path, force: bool) -> None:
    emit(tmp_path, [call(), ui()], "run-1")
    unread = {"argus-assay": ["src/new_syntax.py"]}
    rep = emit(tmp_path, [], "run-2", unread=unread, force=force)
    assay, composer = (architecture_findings([v])[0].id for v in (call(), ui()))
    assert load_pack(tmp_path, assay)["status"] == "open" and rep["unknown"] == [assay] and rep["unread"] == unread
    assert load_pack(tmp_path, composer)["status"] == "obsolete"  # a repo read whole still closes
    assert emit(tmp_path, [], "run-3")["closed"] == 1 and load_pack(tmp_path, assay)["status"] == "obsolete"


def test_an_unread_repo_still_closes_duplicates(tmp_path: Path) -> None:
    emit_packs_from_findings([finding_from_violation(call())], output_dir=tmp_path, kind="architecture", run_id="old")
    assert emit(tmp_path, [call()], "run-1", unread={"argus-assay": ["x.py"]})["by_rule"] == {"duplicate": 1}
