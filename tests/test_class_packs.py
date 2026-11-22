"""Class packs (#54): threshold, idempotent emit, guard proof, child closure rules, the gate."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from auditor import class_packs as cp
from auditor import trust_eval
from auditor.agent_pack import build_pack_bundle, load_pack, packs_dir, rebuild_index
from auditor.trust_metrics import PackView, Scan


def child(out: Path, pid: str, cls: str = "ssrf", repo: str = "r1", *, status: str = "open", kind: str = "nightly",
          path: str = "app/a.py", line: int = 10, title: str = "", snippet: str = "") -> None:
    d = packs_dir(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.yaml").write_text(json.dumps({
        "work_item_id": pid, "kind": kind, "status": status, "priority": {"severity": "HIGH", "score": 0.7},
        "repo": {"name": repo}, "location": {"file_path": path, "line": line, "code_snippet": snippet},
        "narrative": {"title": title or f"{pid} lets the caller pick the URL"},
        "governance": {"finding_class": {"id": cls, "guard": "helper"}} if cls else {}}))


def gov(out: Path, pid: str) -> dict:
    return (load_pack(out, pid) or {}).get("governance") or {}


# ── emission ─────────────────────────────────────────────────────────────────

def test_threshold_one_repo_needs_five(tmp_path: Path) -> None:
    for i in range(4):
        child(tmp_path, f"p{i}")
    assert cp.emit(tmp_path)["class_packs"] == []
    child(tmp_path, "p4")
    assert cp.emit(tmp_path)["class_packs"] == ["pack-class-ssrf"]


def test_two_repos_qualify_with_any_count_and_unguarded_classes_never_do(tmp_path: Path) -> None:
    child(tmp_path, "a", repo="r1")
    child(tmp_path, "b", repo="r2")
    for i in range(6):
        child(tmp_path, f"x{i}", cls="race_condition")  # guard: none
    child(tmp_path, "closed", repo="r3", status="resolved")
    rep = cp.emit(tmp_path)
    assert rep["class_packs"] == ["pack-class-ssrf"] and rep["linked"] == 2
    g = gov(tmp_path, "pack-class-ssrf")
    assert g["children"] == ["a", "b"] and g["class_id"] == "ssrf" and g["guard_type"] == "helper"
    assert gov(tmp_path, "a")["class_parent"] == "pack-class-ssrf" and "class_parent" not in gov(tmp_path, "x0")
    assert "class_parent" not in gov(tmp_path, "closed")


def test_children_without_a_repo_are_not_a_phantom_repo(tmp_path: Path) -> None:
    child(tmp_path, "a", repo="r1")
    child(tmp_path, "b", repo="")
    assert not cp.qualifies([{"repo": {"name": "r1"}}, {"repo": {}}], min_instances=5, min_repos=2)
    assert cp.emit(tmp_path)["class_packs"] == []


def test_explicit_zero_thresholds_are_honoured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_CLASS_PACK_MIN", "50")
    child(tmp_path, "a", repo="r1")
    assert cp.emit(tmp_path)["class_packs"] == []
    assert cp.emit(tmp_path, min_instances=0)["class_packs"] == ["pack-class-ssrf"]


def test_re_emit_is_idempotent_and_keeps_guards_and_status(tmp_path: Path) -> None:
    child(tmp_path, "a", repo="r1")
    child(tmp_path, "b", repo="r2")
    cp.emit(tmp_path)
    cp.record_guard(tmp_path, "pack-class-ssrf", repo="r1", commit="abcdef1000000000000000000000000000000000", proof_test="tests/test_g.py",
                    fixtures=["tests/fx/bad.py"])
    f = packs_dir(tmp_path) / "pack-class-ssrf.yaml"
    before = f.read_text()
    again = cp.emit(tmp_path)
    assert again["linked"] == 0 and f.read_text() == before          # nothing rewritten
    child(tmp_path, "c", repo="r3")
    cp.emit(tmp_path)
    data = load_pack(tmp_path, "pack-class-ssrf")
    assert data["status"] == "in_progress" and "r1" in data["governance"]["class_guard"]  # survives a change
    assert data["governance"]["children"] == ["a", "b", "c"]


def test_child_that_changes_class_is_unlinked(tmp_path: Path) -> None:
    child(tmp_path, "a", repo="r1")
    child(tmp_path, "b", repo="r2")
    cp.emit(tmp_path)
    from auditor.agent_pack import AgentCodingPack, write_pack

    p = AgentCodingPack.from_dict(load_pack(tmp_path, "b"))
    p.governance.pop("finding_class")       # the classifier dropped it; the link is still there
    write_pack(p, tmp_path, preserve_status=False)
    assert cp.emit(tmp_path)["unlinked"] == 1 and "class_parent" not in gov(tmp_path, "b")


def test_class_packs_are_not_findings_and_lead_the_bundle(tmp_path: Path) -> None:
    child(tmp_path, "a", repo="r1")
    child(tmp_path, "b", repo="r2")
    cp.emit(tmp_path)
    rebuild_index(tmp_path)
    assert PackView(id="x", kind=cp.KIND, repo="", path="", line=0, title="", status="open",
                    detected_at=None, run_id="").is_finding is False
    from auditor import pack_classes
    assert all(d.get("kind") != cp.KIND for d in pack_classes.finding_packs(tmp_path))
    b = build_pack_bundle(tmp_path, repo="r2", token_budget=20000)
    assert b["specialists"][0]["work_item_id"] == "pack-class-ssrf"
    brief = (packs_dir(tmp_path) / "b.md").read_text() if (packs_dir(tmp_path) / "b.md").exists() else ""
    assert "pack-class-ssrf" in brief


@pytest.mark.parametrize("kw,msg", [
    ({"commit": "zz-not-hex"}, "hex sha"),
    ({"commit": "3a7fa9d0"}, "full 40-char"),  # a prefix (here a mistyped one) would wait forever as pending
    ({"proof_test": "../escape.py"}, "inside the repo"),
    ({"proof_test": ""}, "required"),
])
def test_record_guard_validates(tmp_path: Path, kw: dict, msg: str) -> None:
    child(tmp_path, "a", repo="r1")
    child(tmp_path, "b", repo="r2")
    cp.emit(tmp_path)
    args = {"repo": "r1", "commit": "abcdef1000000000000000000000000000000000", "proof_test": "tests/test_g.py", "fixtures": [], **kw}
    with pytest.raises(ValueError, match=msg):
        cp.record_guard(tmp_path, "pack-class-ssrf", **args)
    with pytest.raises(LookupError):
        cp.record_guard(tmp_path, "a", repo="r1", commit="abcdef1000000000000000000000000000000000", proof_test="t.py", fixtures=[])


# ── proving a guard ──────────────────────────────────────────────────────────

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def mirror(tmp_path: Path) -> SimpleNamespace:
    m = tmp_path / "mirror"
    m.mkdir()
    git(m, "init", "-q", "-b", "dev")
    git(m, "config", "user.email", "t@t")
    git(m, "config", "user.name", "t")
    (m / "app.py").write_text("x = 1\n")
    git(m, "add", ".")
    git(m, "commit", "-qm", "base")
    (m / "tests").mkdir()
    (m / "tests" / "test_guard.py").write_text("def test_fires():\n    assert True\n")
    (m / "tests" / "bad.py").write_text("requests.get(url)\n")
    git(m, "add", ".")
    git(m, "commit", "-qm", "guard")
    return SimpleNamespace(path=m, guard=git(m, "rev-parse", "HEAD"))


def fake_runner(patched: dict, base_outcome: str):
    def run(tree: Path, base, tests):
        assert (tree / "tests/test_guard.py").exists()
        before = base()
        assert before is not None and not (before / "tests/test_guard.py").exists()  # the tree before the guard
        b = dict.fromkeys(patched, base_outcome)
        return SimpleNamespace(patched=patched, repro=sorted(t for t, s in patched.items()
                                                              if s == "passed" and b[t] == "failed"), notes=[])
    return run


def rec(sha: str, **kw) -> dict:
    return {"commit": sha, "proof_test": "tests/test_guard.py", "fixtures": ["tests/bad.py"], **kw}


def test_proven_only_when_the_test_fails_without_the_guard(mirror) -> None:
    ok = cp.prove(rec(mirror.guard), mirror.path, fake_runner({"tests/test_guard.py::test_fires": "passed"}, "failed"))
    assert ok["status"] == "proven" and ok["repro"] and ok["merged_at"]
    vacuous = cp.prove(rec(mirror.guard), mirror.path,
                       fake_runner({"tests/test_guard.py::test_fires": "passed"}, "passed"))
    assert vacuous["status"] == "failed" and "without the guard" in vacuous["note"]
    broken = cp.prove(rec(mirror.guard), mirror.path, fake_runner({"tests/test_guard.py::t": "failed"}, "failed"))
    assert broken["status"] == "failed" and "failing on dev" in broken["note"]


def test_not_proven_before_merge_or_without_the_files(mirror) -> None:
    git(mirror.path, "checkout", "-q", "-b", "feature")
    (mirror.path / "f.txt").write_text("x")
    git(mirror.path, "add", ".")
    git(mirror.path, "commit", "-qm", "unmerged")
    side = git(mirror.path, "rev-parse", "HEAD")
    git(mirror.path, "checkout", "-q", "dev")
    def never(*a):
        pytest.fail("ran")

    assert cp.prove(rec(side), mirror.path, never)["status"] == "pending"
    assert cp.prove(rec("1234567deadbeef"), mirror.path, never)["status"] == "pending"
    missing = cp.prove(rec(mirror.guard, fixtures=["tests/gone.py"]), mirror.path, never)
    assert missing["status"] == "failed" and "tests/gone.py" in missing["note"]
    assert cp.prove(rec(mirror.guard), None, never)["status"] == "pending"


@pytest.mark.parametrize("bad", ["../outside_test.py", "/abs/test_x.py", "tests/../../outside_test.py"])
def test_guard_paths_outside_the_export_fail_and_never_run(mirror, bad: str) -> None:
    host = mirror.path.parent  # a host file next to the mirror, never in the exported tree
    for name in ("outside_test.py", "abs_test.py"):
        (host / name).write_text("def test_x():\n    assert True\n")
    path = str(host / "abs_test.py") if bad.startswith("/") else bad

    def never(*a):
        pytest.fail("ran")

    for r in (rec(mirror.guard, proof_test=path), rec(mirror.guard, fixtures=[path])):
        res = cp.prove(r, mirror.path, never)
        assert res["status"] == "failed" and "outside the repo" in res["note"]


# ── closing children: only what the guard's own rule covers ─────────────────

NOW = datetime(2026, 11, 16, tzinfo=UTC)
RULE = {"tool": "ruff", "select": ["B905"]}
ZIP_TEXT = "zip() silently truncates clips when raw_paths and names differ in length"

BEFORE_SRC = """def build(raw_paths, names, a1, b1):
    clips = []
    for path, name in zip(raw_paths, names):
        clips.append((path, name))
    pairs = list(zip(a1, b1))
    return clips, pairs
"""


def view(kind: str = "diff_audit", **kw) -> PackView:
    return PackView(**{"id": "a", "kind": kind, "repo": "r1", "path": "app/clips.py", "line": 1,
                       "title": ZIP_TEXT, "status": "open", "detected_at": NOW, "run_id": "", **kw})


def scan(at: datetime, findings: list[dict]) -> Scan:
    return Scan(run_id=f"run-{at:%d%H}", repo="r1", at=at, findings=findings)


SAME = {"path": "app/clips.py", "line": 3, "title": ZIP_TEXT, "type": "bug"}


def zip_mirror(tmp_path: Path, guarded_src: str) -> SimpleNamespace:
    """A dev mirror: the zip code, then a guard commit that rewrites it to *guarded_src*."""
    from auditor.pack_gc import Mirror

    m = tmp_path / "zm"
    (m / "app").mkdir(parents=True)
    git(m, "init", "-q", "-b", "dev")
    git(m, "config", "user.email", "t@t")
    git(m, "config", "user.name", "t")
    (m / "app" / "clips.py").write_text(BEFORE_SRC)
    git(m, "add", ".")
    git(m, "commit", "-qm", "base")
    (m / "app" / "clips.py").write_text(guarded_src)
    (m / "tests").mkdir()
    (m / "tests" / "test_guard.py").write_text("def test_fires():\n    assert True\n")
    (m / "tests" / "bad.py").write_text("zip(a, b)\n")
    git(m, "add", ".")
    git(m, "commit", "-qm", "guard")
    sha = git(m, "rev-parse", "HEAD")
    return SimpleNamespace(path=m, guard=sha, mirror=Mirror(m), rec={"commit": sha, "rule": RULE})


FIXED = BEFORE_SRC.replace("zip(raw_paths, names)", "zip(raw_paths, names, strict=True)").replace(
    "zip(a1, b1)", "zip(a1, b1, strict=False)")
PROOF = {"status": "proven", "merged_at": (NOW - timedelta(days=1)).isoformat()}
AFTER = [scan(NOW + timedelta(days=1), [])]


def test_parse_rule() -> None:
    assert cp.parse_rule("ruff: s110, B905,B905") == {"tool": "ruff", "select": ["B905", "S110"]}
    assert cp.parse_rule("") is None
    for bad in ("B905", "semgrep:x", "ruff:", "ruff:B905;rm -rf"):
        with pytest.raises(ValueError, match="rule must be"):
            cp.parse_rule(bad)


def test_record_guard_stores_the_rule(tmp_path: Path) -> None:
    child(tmp_path, "a", repo="r1")
    child(tmp_path, "b", repo="r2")
    cp.emit(tmp_path)
    g = cp.record_guard(tmp_path, "pack-class-ssrf", repo="r1", commit="abcdef1000000000000000000000000000000000",
                        proof_test="tests/test_g.py", fixtures=[], rule="ruff:B905")
    assert g["rule"] == {"tool": "ruff", "select": ["B905"]}
    assert "rule" not in cp.record_guard(tmp_path, "pack-class-ssrf", repo="r2", proof_test="t.py", fixtures=[],
                                         commit="abcdef1000000000000000000000000000000000")


def test_attribution_needs_the_findings_own_identifiers() -> None:
    lines = BEFORE_SRC.splitlines()
    hit = cp.RuleHit(3, "B905", lines[2])
    assert cp.rule_attributed(hit, lines, ZIP_TEXT) == {"raw_paths", "names"}
    other = cp.RuleHit(5, "B905", lines[4])
    assert cp.rule_attributed(other, lines, ZIP_TEXT) == set()            # same rule, another zip
    assert cp.rule_attributed(hit, lines, "the path and name can differ") == set()  # nothing distinctive
    # names in comments or strings never count
    commented = ["x = 1  # raw_paths and names", "for p in zip(a, b):"]
    assert cp.rule_attributed(cp.RuleHit(2, "B905", commented[1]), commented, ZIP_TEXT) == set()
    # one distinctive name is enough when the hit's code is in the cited snippet
    snip = "  3|     for path, name in zip(raw_paths, names):"
    assert cp.rule_attributed(hit, lines, "raw_paths may be short", snip) == {"raw_paths"}
    assert cp.rule_attributed(hit, lines, "raw_paths may be short") == set()


def test_rule_hits_ignores_noqa_and_never_reads_a_failure_as_clean(monkeypatch) -> None:
    src = "def f(a, b):\n    return list(zip(a, b))  # noqa: B905 lengths match by construction\n"
    assert [(h.line, h.code) for h in cp.rule_hits(src, "m.py", RULE)] == [(2, "B905")]
    assert cp.rule_hits(src, "m.py", {"tool": "semgrep", "select": ["x"]}) is None
    monkeypatch.setattr(cp.shutil, "which", lambda _x: None)
    monkeypatch.setattr(cp.sys, "executable", "/nonexistent/python")
    assert cp.rule_hits(src, "m.py", RULE) is None


def test_child_closes_only_when_its_rule_hit_is_gone(tmp_path: Path) -> None:
    z = zip_mirror(tmp_path, FIXED)
    verdict, why = cp.judge_child(view(), PROOF, AFTER, z.mirror, z.rec, ZIP_TEXT)
    assert verdict == "close" and "B905 at app/clips.py:3" in why and "raw_paths" in why
    assert cp.judge_child(view(), PROOF, [], z.mirror, z.rec, ZIP_TEXT)[0] == "wait"   # no scan since the guard
    assert cp.judge_child(view(), PROOF, [scan(NOW + timedelta(days=1), [SAME])], z.mirror, z.rec,
                          ZIP_TEXT)[0] == "exception"
    # the scan's silence alone closes nothing: a finding the rule never fired on stays open
    assert cp.judge_child(view(title="Upload size is unbounded"), PROOF, AFTER, z.mirror, z.rec,
                          "Upload size is unbounded in read_upload")[0] == "unattributed"
    assert cp.judge_child(view(), PROOF, AFTER, z.mirror, {"commit": z.guard}, ZIP_TEXT)[0] == "unattributed"
    assert cp.judge_child(view(), PROOF, AFTER, z.mirror, z.rec, ZIP_TEXT,
                          runner=lambda *a: None)[0] == "wait"                       # rule couldn't run


def test_a_waived_hit_is_not_a_closure(tmp_path: Path) -> None:
    waived = BEFORE_SRC.replace("zip(raw_paths, names):", "zip(raw_paths, names):  # noqa: B905 caller pairs them")
    z = zip_mirror(tmp_path, waived)
    verdict, why = cp.judge_child(view(), PROOF, AFTER, z.mirror, z.rec, ZIP_TEXT)
    assert verdict == "exception" and "still fires" in why


def test_a_deleted_file_closes_an_attributed_child(tmp_path: Path) -> None:
    z = zip_mirror(tmp_path, FIXED)
    git(z.path, "rm", "-q", "app/clips.py")
    git(z.path, "commit", "-qm", "drop")
    from auditor.pack_gc import Mirror

    assert cp.judge_child(view(), PROOF, AFTER, Mirror(z.path), z.rec, ZIP_TEXT)[0] == "close"


def test_run_closes_attributed_children_only(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "reports"
    z = zip_mirror(tmp_path, FIXED)
    child(out, "a", cls="silent_failure", repo="r1", kind="diff_audit", path="app/clips.py", line=1, title=ZIP_TEXT)
    child(out, "b", cls="silent_failure", repo="r1", kind="nightly", path="app/clips.py", line=40,
          title="Webhook failures are swallowed")
    child(out, "c", cls="silent_failure", repo="r1", kind="diff_audit", path="app/clips.py", line=1,
          title="pairs drops entries when a1 and b1 differ")
    child(out, "d", cls="silent_failure", repo="r2")
    cp.emit(out)
    cp.record_guard(out, "pack-class-silent-failure", repo="r1", commit=z.guard, proof_test="tests/test_guard.py",
                    fixtures=["tests/bad.py"], rule="ruff:B905")
    monkeypatch.setattr("auditor.pack_gc.mirror_paths", lambda *_: {"r1": z.path})
    later = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(cp, "_scans_by_repo", lambda *_: {"r1": [scan(later, [])]})
    runner = fake_runner({"tests/test_guard.py::test_fires": "passed"}, "failed")

    dry = cp.run(out, runner=runner, dry_run=True)
    assert [c["pack"] for c in dry["closed"]] == ["a"] and load_pack(out, "a")["status"] == "open"

    rep = cp.run(out, runner=runner)
    # "c" names a1/b1, but neither is distinctive, so it isn't attributed: precision over recall
    assert [c["pack"] for c in rep["closed"]] == ["a"] and rep["unattributed"] == 2 and not rep["exceptions"]
    a = load_pack(out, "a")
    assert a["status"] == "resolved" and a["verification"]["evidence"]["rule"] == {"tool": "ruff", "select": ["B905"]}
    assert a["status_history"][-1]["note"].startswith("class_guard (pack-class-silent-failure): rule B905")
    for pid in ("b", "c", "d"):
        assert load_pack(out, pid)["status"] == "open" and "class_exception" not in gov(out, pid)
    assert cp.lifecycle_metric(out)["n"] == 1

    # Once nothing's left open, the class pack resolves itself.
    for pid, repo in (("b", "r1"), ("c", "r1"), ("d", "r2")):
        child(out, pid, cls="silent_failure", repo=repo, status="wontfix")
    cp.run(out, runner=lambda *a: pytest.fail("proof is not re-run once proven"))
    assert load_pack(out, "pack-class-silent-failure")["status"] == "resolved"


def test_lifecycle_gate_counts_rule_attributed_closures_only(tmp_path: Path) -> None:
    out = tmp_path
    d = packs_dir(out)
    d.mkdir(parents=True)
    ruled = [{"status": "resolved", "note": "class_guard (pack-class-ssrf): rule B905 at a.py:3 (x_y) fired",
              "at": "2026-11-01T00:00:00+00:00"}]
    silent = [{"status": "resolved", "note": "class_guard (pack-class-ssrf): not reported by scan s",
               "at": "2026-11-01T00:00:00+00:00"}]
    for i in range(12):
        later = [{"status": "open", "note": "reported again", "at": "2026-11-02T00:00:00+00:00"}] if i == 0 else []
        (d / f"k{i}.yaml").write_text(json.dumps({
            "work_item_id": f"k{i}", "kind": "nightly", "status": "open" if i == 0 else "resolved",
            "repo": {"name": "r"}, "location": {"file_path": "a.py"}, "narrative": {"title": "t"},
            "status_history": (silent if i >= 10 else ruled) + later}))
    m = cp.lifecycle_metric(out)
    assert (m["n"], m["held"], m["rate"]) == (10, 9, 0.9)      # the two silence-only closures don't count
    g = {x.id: x for x in trust_eval.build_gates(None, None, None, m)}["class_packs.lifecycle"]
    assert g.status == "fail" and g.n == 10
    assert {x.id: x for x in trust_eval.build_gates(None, None, None, {"n": 0})}["class_packs.lifecycle"].status \
        == "insufficient"


def test_board_filters_by_class_and_page_lists_children(tmp_path: Path) -> None:
    from auditor.agent_pack import AgentCodingPack
    from auditor.ui_repos import _class_meta, packs_board

    child(tmp_path, "a", repo="r1")
    child(tmp_path, "b", repo="r2")
    child(tmp_path, "z", repo="r1", cls="injection")
    cp.emit(tmp_path)
    rebuild_index(tmp_path)
    board = packs_board(tmp_path, cls="ssrf")
    ids = {pk["id"] for col in board["columns"] for pk in col["packs"]}
    assert ids == {"a", "b", "pack-class-ssrf"} and "ssrf" in board["facets"]["classes"]
    card = next(pk for col in board["columns"] for pk in col["packs"] if pk["id"] == "a")
    assert card["class_parent"] == "pack-class-ssrf" and card["finding_class"] == "ssrf"
    meta = _class_meta(tmp_path, AgentCodingPack.from_dict(load_pack(tmp_path, "pack-class-ssrf")))
    assert [k["id"] for k in meta["children"]] == ["a", "b"] and meta["guard_type"] == "helper"
    assert _class_meta(tmp_path, AgentCodingPack.from_dict(load_pack(tmp_path, "a")))["class_parent"] == "pack-class-ssrf"


def test_an_except_hit_is_attributed_through_its_try_body() -> None:
    lines = ["def load(state_path):", "    try:", "        data = read_state(state_path)",
             "    except Exception:", "        pass", "    x = parse_header(blob)", "    try:", "        y = 1",
             "    except Exception:", "        pass"]
    text = "Unreadable state file is silently reset: read_state(state_path) errors are swallowed"
    assert cp.rule_attributed(cp.RuleHit(4, "S110", lines[3]), lines, text) == {"read_state", "state_path"}
    assert cp.rule_attributed(cp.RuleHit(9, "S110", lines[8]), lines, text) == set()   # another try entirely
