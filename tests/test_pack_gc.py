"""Pack GC (#67): stale open finding packs close as obsolete; a re-found issue reopens its pack."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from auditor import pack_gc
from auditor import trust_metrics as tm
from auditor.agent_pack import emit_packs_from_findings, load_pack, update_pack_status
from auditor.contracts import Finding
from auditor.pack_gc import Mirror, cited_core, code_still_there, plan

T0 = datetime(2026, 11, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=5)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def snippet(start: int, lines: list[str]) -> str:
    return "\n".join(f"{start + i:>5}| {ln}" for i, ln in enumerate(lines))


CODE = ["def add(a, b):", "    total = a - b  # bug", "    log(total)", "    return total", ""]


def pack(pid: str, *, kind: str = "nightly", status: str = "open", path: str = "app/calc.py", line: int = 2,
         title: str = "add subtracts instead of adding", seen: float = 0, snip: str | None = None,
         repo: str = "x", **gov) -> tm.PackView:
    return tm.pack_view({
        "work_item_id": pid, "kind": kind, "status": status, "repo": {"name": repo},
        "location": {"file_path": path, "line": line, "code_snippet": snippet(1, CODE) if snip is None else snip},
        "narrative": {"title": title}, "provenance": {"detected_at": at(seen).isoformat(), "run_id": "r0"},
        "governance": gov,
    })


def scan(run: str, hours: float, findings: list[tuple[str, str, int]] = (), repo: str = "x") -> tm.Scan:
    return tm.Scan(run_id=run, repo=repo, at=at(hours),
                   findings=[{"path": p, "title": t, "line": ln, "type": "bug"} for p, t, ln in findings])


class FakeMirror(Mirror):
    def __init__(self, files: dict[str, list[str]], changes: dict[str, datetime] | None = None):
        self.path, self.ref = Path("/m"), "origin/dev"
        self._files, self._text = set(files), dict(files)
        self._changes = changes or {}

    def last_change(self, path: str) -> datetime | None:
        return self._changes.get(path)


FIXED = ["def add(a, b):", "    return a + b", ""]


def ids(result: dict) -> dict[str, str]:
    return {c.pack_id: c.rule for c in result["closures"]}


# ── cited code ───────────────────────────────────────────────────────────────

def test_cited_core_and_search() -> None:
    core = cited_core(snippet(1, CODE), 2)
    assert core == ["def add(a, b):", "total = a - b # bug", "log(total)", "return total"]
    moved = ["# header", "", "", *CODE]  # same code further down, blank lines around: still there
    assert code_still_there(core, moved) and not code_still_there(core, FIXED)
    assert cited_core(snippet(1, ["}", ")", "", "]"]), 2) == []  # nothing distinctive to look for
    assert cited_core("no numbered lines here", 2) == []


# ── rules ────────────────────────────────────────────────────────────────────

def test_file_gone_closes_any_open_finding_pack_including_ask() -> None:
    m = {"x": FakeMirror({"other.py": ["x"]})}
    r = plan([pack("n"), pack("a", kind="ask"), pack("d", kind="diff_audit")], [], m, now=NOW)
    assert ids(r) == {"n": "file_gone", "a": "file_gone", "d": "file_gone"}


def test_nightly_pack_closes_after_n_consecutive_misses() -> None:
    m = {"x": FakeMirror({"app/calc.py": CODE})}
    p = pack("n", seen=1)
    two = [scan("s1", 10), scan("s2", 20)]
    assert ids(plan([p], two, m, now=NOW, misses=3)) == {}
    three = [*two, scan("s3", 30)]
    r = plan([p], three, m, now=NOW, misses=3)
    assert ids(r) == {"n": "not_rescanned"} and "detector drift" in r["closures"][0].reason
    # reported in any of the last three: stays open
    hit = [scan("s1", 10), scan("s2", 20, [("app/calc.py", "add subtracts instead of adding", 2)]), scan("s3", 30)]
    assert ids(plan([p], hit, m, now=NOW, misses=3)) == {}


def test_not_rescanned_says_when_the_file_changed() -> None:
    m = {"x": FakeMirror({"app/calc.py": CODE}, {"app/calc.py": at(15)})}
    r = plan([pack("n", seen=1)], [scan("s1", 10), scan("s2", 20), scan("s3", 30)], m, now=NOW, misses=3)
    assert "the file changed since" in r["closures"][0].reason


def test_code_changed_needs_a_silent_scan_for_scanned_files() -> None:
    m = {"x": FakeMirror({"app/calc.py": FIXED})}
    d = pack("d", kind="diff_audit", seen=1)
    assert ids(plan([d], [], m, now=NOW)) == {}  # code gone, but no nightly scan has looked since
    assert ids(plan([d], [scan("s1", 10)], m, now=NOW)) == {"d": "code_changed"}
    reported = [scan("s1", 10, [("app/calc.py", "add subtracts instead of adding", 2)])]
    assert ids(plan([d], reported, m, now=NOW)) == {}
    # code still there (merely moved): never closed on the scan's silence alone
    moved = {"x": FakeMirror({"app/calc.py": ["# moved", *CODE]})}
    assert ids(plan([d], [scan("s1", 10), scan("s2", 20), scan("s3", 30)], moved, now=NOW)) == {}


def test_code_changed_in_a_file_the_scanner_never_reads() -> None:
    doc = ["## Config", "Set AUDIT_API_ALLOWED_HOSTS=* to allow any host", "and restart.", ""]
    p = pack("d", kind="diff_audit", path="docs/config.md", line=2, snip=snippet(1, doc), seen=1)
    m = {"x": FakeMirror({"docs/config.md": ["## Config", "Hosts must be listed explicitly.", ""]})}
    r = plan([p], [], m, now=NOW)
    assert ids(r) == {"d": "code_changed"} and "doesn't read" in r["closures"][0].reason
    assert pack_gc.nightly_covers("src/a.py") and not pack_gc.nightly_covers("docs/config.md")
    assert not pack_gc.nightly_covers("web/node_modules/x/index.js")


def test_diff_audit_ttl_and_ask_never_expires() -> None:
    m = {"x": FakeMirror({"app/calc.py": CODE})}
    old = NOW + timedelta(days=40)
    r = plan([pack("d", kind="diff_audit"), pack("a", kind="ask"), pack("n")], [], m, now=old, ttl_days=30)
    assert ids(r) == {"d": "expired"}


def test_only_open_finding_packs_are_considered() -> None:
    m = {"x": FakeMirror({})}  # every file gone
    packs = [pack(f"s-{st}", status=st) for st in ("claimed", "in_progress", "unverified", "resolved", "wontfix")]
    packs += [pack("w", kind="world_model"), pack("arch", kind="architecture"), pack("dup", duplicate_of="s-claimed")]
    r = plan(packs, [], m, now=NOW)
    assert ids(r) == {} and r["open_considered"] == 0


def test_missing_mirror_only_disables_mirror_rules() -> None:
    p = pack("n", seen=1)
    r = plan([p], [scan("s1", 10), scan("s2", 20), scan("s3", 30)], {}, now=NOW, misses=3)
    assert ids(r) == {"n": "not_rescanned"}


# ── safety ───────────────────────────────────────────────────────────────────

def test_broken_latest_scan_skips_the_repo_but_not_file_gone() -> None:
    m = {"x": FakeMirror({"app/calc.py": CODE})}
    scans = [scan("s1", 10, [("app/other.py", "something", 1)]), scan("s2", 20, [("app/other.py", "something", 1)]),
             scan("s3", 30)]  # latest reports nothing at all
    r = plan([pack("n", seen=1), pack("g", path="gone.py")], scans, m, now=NOW, misses=2)
    assert ids(r) == {"g": "file_gone"} and "reports nothing" in r["skipped"]["x"]


def test_mass_closure_is_held_unless_forced() -> None:
    m = {"x": FakeMirror({})}
    packs = [pack(f"p{i}", path=f"f{i}.py") for i in range(12)] + [pack("keep", path="app/calc.py")]
    m["x"]._files = {"app/calc.py"}
    r = plan(packs, [], m, now=NOW)
    assert ids(r) == {} and "would close 12 of 13" in r["held"]["x"]
    assert len(plan(packs, [], m, now=NOW, force=True)["closures"]) == 12
    few = [pack(f"p{i}", path=f"f{i}.py") for i in range(3)]
    assert len(plan(few, [], m, now=NOW)["closures"]) == 3  # small numbers are never held


# ── end to end: apply, reopen, metrics, CLI/API ──────────────────────────────

def _git(cwd: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def live(tmp_path: Path, monkeypatch, memdb):
    """A mirror (git repo) whose dev lacks one file, packs for it, and a mirrors config."""
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    repo = tmp_path / "mirror" / "x"
    repo.mkdir(parents=True)
    (repo / "keep.py").write_text("def ok():\n    return 1\n")
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    mirrors = tmp_path / "mirrors.yaml"
    mirrors.write_text(f"mirrors:\n  - name: x\n    path: {repo}\n")
    out = tmp_path / "reports"

    def f(path: str, title: str, line: int = 1) -> Finding:
        return Finding(id=f"llm:{path}:{line}", type="bug", severity="HIGH", repo="x", file_path=f"/m/x/{path}",
                       line=line, title=title, description="d", evidence="e", recommendation="r", source="llm")
    [gone] = emit_packs_from_findings([f("deleted.py", "Pickle loaded with weights_only=False")],
                                      output_dir=out, kind="nightly", run_id="run-1")
    [kept] = emit_packs_from_findings([f("keep.py", "Return value ignored by caller")],
                                      output_dir=out, kind="nightly", run_id="run-1")
    return {"out": out, "mirrors": mirrors, "gone": gone.work_item_id, "kept": kept.work_item_id, "f": f}


def test_apply_closes_records_and_reopens_on_comeback(live) -> None:
    out = live["out"]
    dry = pack_gc.run(out, dry_run=True, mirrors_path=live["mirrors"])
    assert [c["pack_id"] for c in dry["closures"]] == [live["gone"]] and dry["closed"] == 0
    assert load_pack(out, live["gone"])["status"] == "open" and not (out / pack_gc.REPORT_NAME).exists()

    rep = pack_gc.run(out, mirrors_path=live["mirrors"])
    assert rep["closed"] == 1 and (out / pack_gc.REPORT_NAME).exists()
    data = load_pack(out, live["gone"])
    assert data["status"] == "obsolete" and data["status_note"].startswith("gc (file_gone)")
    assert data["governance"]["gc"]["rule"] == "file_gone"
    assert load_pack(out, live["kept"])["status"] == "open"
    assert pack_gc.run(out, mirrors_path=live["mirrors"])["closed"] == 0  # idempotent

    # the issue is reported again (reworded, next run): the same pack reopens, no twin
    [back] = emit_packs_from_findings([live["f"]("deleted.py", "Pickle loaded with weights_only=False again", 3)],
                                      output_dir=out, kind="nightly", run_id="run-2")
    assert back.work_item_id == live["gone"]
    data = load_pack(out, live["gone"])
    assert data["status"] == "open" and "reported again after pack GC" in data["status_note"]
    g = tm.gc_metric(tm.load_packs(out))
    assert (g["closed"], g["reopened"], g["rate"]) == (1, 1, 1.0) and g["by_rule"]["file_gone"]["reopened"] == 1


def test_exact_id_reemit_also_reopens(live) -> None:
    out = live["out"]
    update_pack_status(out, live["kept"], "obsolete", "gc (not_rescanned): test")
    [again] = emit_packs_from_findings([live["f"]("keep.py", "Return value ignored by caller")],
                                       output_dir=out, kind="nightly", run_id="run-1")
    assert again.work_item_id == live["kept"] and load_pack(out, live["kept"])["status"] == "open"


def test_obsolete_is_closed_work(live) -> None:
    from auditor.agent_pack import ACTIVE_PACK_STATUSES, build_pack_bundle

    pack_gc.run(live["out"], mirrors_path=live["mirrors"])
    assert "obsolete" not in ACTIVE_PACK_STATUSES
    b = build_pack_bundle(live["out"], repo="x")
    assert live["gone"] not in {s["work_item_id"] for s in b["specialists"]}


def test_api_is_dry_run_by_default_and_needs_auth(live, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from auditor.audit_api import app

    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(live["out"]))
    monkeypatch.setattr(pack_gc, "mirror_paths", lambda *_a, **_k: {"x": live["mirrors"].parent / "mirror" / "x"})
    monkeypatch.delenv("AI_AUDIT_API_TOKEN", raising=False)
    client = TestClient(app)
    body = client.post("/audit/packs/gc").json()
    assert body["dry_run"] is True and body["planned"] == 1 and body["closed"] == 0
    assert load_pack(live["out"], live["gone"])["status"] == "open"
    assert client.post("/audit/packs/gc?dry_run=0").json()["closed"] == 1
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "secret")
    assert client.post("/audit/packs/gc").status_code in (401, 403)


def test_run_quietly_toggle_and_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_PACK_GC", "0")
    assert "skipped" in pack_gc.run_quietly(tmp_path)
    monkeypatch.setenv("AI_AUDIT_PACK_GC", "1")
    monkeypatch.setattr(pack_gc, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pack_gc.run_quietly(tmp_path) == {"error": "boom"}
