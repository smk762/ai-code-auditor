"""The hosted demo's tape (#199): nothing dropped, nothing private, every fix backed by its commit."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from auditor import demo_tape
from auditor.demo_tape import TapeError

ROOT = Path(__file__).resolve().parent.parent
REPO = "argus-scout"
URL = "https://github.com/smk762/argus-scout"


def _pack(pid: str, status: str = "resolved", kind: str | None = "repro", **narrative) -> dict:
    ev = {"kind": kind, "reason": "1 test(s) fail on dev and pass with the fix: tests/test_x.py::test_x",
          "base_sha": "a" * 40, "at": "2026-11-24T04:48:00+00:00",
          "repro": {"base": {"tests/test_x.py::test_x": "failed", "tests/test_x.py::test_y": "passed"},
                    "patched": {"tests/test_x.py::test_x": "passed", "tests/test_x.py::test_y": "passed"},
                    "repro": ["tests/test_x.py::test_x"]}} if kind else {}
    if kind == "waiver":
        ev["waiver"] = "Test-only finding: the stronger test passes on dev's source too."
    return {
        "work_item_id": pid, "status": status, "status_note": "Wrong: the helper defines it." if status == "wontfix" else "",
        "repo": {"name": REPO, "path": f"/home/someone/mirrors/{REPO}"},
        "priority": {"severity": "MEDIUM"},
        "location": {"file_path": f"/home/someone/mirrors/{REPO}/src/x.py", "line": 3, "code_snippet": "  3| x = 1"},
        "narrative": {"title": narrative.get("title", f"title {pid}"), "finding_type": "bug", "description": "d",
                      "recommendation": "r", "acceptance_criteria": [f"Re-check `/home/someone/mirrors/{REPO}/src/x.py`"]},
        "verification": {"evidence": ev},
        "status_history": [{"status": "claimed", "at": "2026-11-24T04:35:00+00:00", "note": ""},
                           {"status": status, "at": "2026-11-24T04:47:00+00:00", "note": ""}],
    }


def _commit(sha: str = "abc1234") -> dict:
    return demo_tape.commit_entry(sha + "0" * 33, "fix: x", "diff --git a/src/x.py b/src/x.py\n+x = 2\n", URL)


def _run(**packs) -> dict:
    return {"repo": REPO, "run_id": "repo-audit-1", "scanned_sha": "b" * 40, "started_at": "2026-11-23T17:30:03Z",
            "pr": {"number": 34, "title": "t", "merged_at": "2026-11-24T08:13:55Z"}, "packs": packs}


def _tape(run, packs, commits=None):
    return demo_tape.build_tape(run, packs, commits if commits is not None else {"abc1234": _commit()},
                                repo_url=URL, recorded_at="2026-11-28T00:00:00Z")


def test_every_listed_pack_is_on_the_tape_and_the_counts_come_from_the_packs():
    packs = [_pack("p1"), _pack("p2", kind="waiver"), _pack("p3", kind="test_only"),
             _pack("p4", status="wontfix", kind=None)]
    tape = _tape(_run(p1="abc1234", p2="abc1234", p3="abc1234", p4=None), packs)
    assert [p["id"] for p in tape["packs"]] == ["p1", "p3", "p2", "p4"]  # ordered by outcome, none dropped
    assert tape["counts"] == {"findings": 4, "resolved": 3, "repro": 1, "test_only": 1, "waiver": 1, "wontfix": 1,
                              "commits": 1}
    wontfix = tape["packs"][-1]
    assert wontfix["note"] == "Wrong: the helper defines it." and wontfix["evidence"] == {}
    assert tape["packs"][2]["evidence"]["waiver"].startswith("Test-only finding")


def test_a_listed_pack_that_is_missing_is_refused_not_skipped():
    with pytest.raises(TapeError, match="aren't in reports/agent_packs"):
        _tape(_run(p1="abc1234", gone="abc1234"), [_pack("p1")])


def test_a_resolved_pack_without_its_commit_is_refused():
    with pytest.raises(TapeError, match="names no commit"):
        _tape(_run(p1=None), [_pack("p1")])


@pytest.mark.parametrize("status,kind", [("open", None), ("unverified", None), ("resolved", "lint_only")])
def test_only_finished_packs_with_evidence_the_site_can_show_are_replayed(status, kind):
    with pytest.raises(TapeError):
        _tape(_run(p1="abc1234"), [_pack("p1", status=status, kind=kind)])


def test_repro_tests_come_first_with_both_results():
    tests = _tape(_run(p1="abc1234"), [_pack("p1")])["packs"][0]["evidence"]["tests"]
    assert tests[0] == {"id": "tests/test_x.py::test_x", "dev": "failed", "patched": "passed", "repro": True}
    assert tests[1]["repro"] is False


def test_paths_into_the_checkout_become_repo_relative():
    tape = _tape(_run(p1="abc1234"), [_pack("p1")])
    p = tape["packs"][0]
    assert p["file"] == "src/x.py"
    assert p["acceptance_criteria"] == ["Re-check `src/x.py`"]
    assert "/home/" not in json.dumps(tape)
    demo_tape.check_public(tape, [], ["argus-private"])


@pytest.mark.parametrize("leak", ["see 192.168.1.20:8765", "at /home/someone/notes", "mail a.b@corp.example.org",
                                  "argus-private does this", "on the gandalf box"])
def test_the_opsec_gate_refuses_the_whole_tape_and_redacts_nothing(leak):
    tape = _tape(_run(p1="abc1234"), [_pack("p1", title=f"Leaks: {leak}")])
    with pytest.raises(TapeError, match="opsec gate refused"):
        demo_tape.check_public(tape, ["gandalf"], ["argus-private"])
    assert leak in json.dumps(tape)  # refused, not rewritten


def test_a_long_diff_is_cut_and_says_by_how_much():
    c = demo_tape.commit_entry("f" * 40, "s", "\n".join(f"+{i}" for i in range(demo_tape.DIFF_MAX_LINES + 7)), URL)
    assert c["diff_cut"] == 7 and len(c["diff"].splitlines()) == demo_tape.DIFF_MAX_LINES


# ── the committed tape and the site built from it ─────────────────────────────

TAPE = ROOT / "demo" / "tape" / "tape.json"
RUN = ROOT / "demo" / "run.yaml"


def test_the_committed_tape_replays_exactly_the_run_it_names():
    tape, run = json.loads(TAPE.read_text()), yaml.safe_load(RUN.read_text())
    assert tape["format"] == demo_tape.FORMAT and tape["run"]["run_id"] == run["run_id"]
    assert sorted(p["id"] for p in tape["packs"]) == sorted(run["packs"])
    assert tape["counts"] == demo_tape.counts(tape["packs"])
    for p in tape["packs"]:
        want = run["packs"][p["id"]]
        assert (p["commit"] or {}).get("sha", "").startswith(str(want)) if want else p["commit"] is None
        if p["outcome"] == "repro":
            assert any(t["repro"] and t["dev"] != "passed" and t["patched"] == "passed" for t in p["evidence"]["tests"])
    demo_tape.check_public(tape, [], [])


def test_the_site_builds_every_pack_page_with_no_inline_script_or_style(tmp_path):
    spec = importlib.util.spec_from_file_location("demo_build", ROOT / "demo" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    tape = json.loads(TAPE.read_text())
    build.build(tmp_path / "out", tape)
    pages = list((tmp_path / "out" / "packs").glob("*/index.html"))
    assert len(pages) == len(tape["packs"])
    assert "script-src 'self'" in (tmp_path / "out" / "_headers").read_text()
    index = (tmp_path / "out" / "index.html").read_text()
    assert index.count('class="tile ') == len(tape["packs"])
    for f in [tmp_path / "out" / "index.html", *pages]:
        html = f.read_text()
        assert not re.search(r"<style\b|<[a-z][^>]*\sstyle=", html), f.name
