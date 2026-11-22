"""Evidence bar (#58): a pass resolves a pack only with proof that fits it; otherwise unverified."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import evidence, validator
from auditor.audit_api import (
    app,  # imported here: it loads .env, which the conftest then overrides
)
from auditor.evidence import ReproResult, classify_changes, decide


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def host_python(monkeypatch):
    """Use this interpreter (it has pytest) instead of building per-project venvs."""
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache_dir, extra=None: (Path(sys.executable), ""))


BUG = {"kind": "nightly", "narrative": {"finding_type": "bug"}}
LEGACY = {"kind": "nightly", "narrative": {}}  # packs from before finding_type was stored
CONTRACT = {"kind": "nightly", "narrative": {"finding_type": "contract"}}
ARCH = {"kind": "architecture", "narrative": {}}


# ── classifying ──────────────────────────────────────────────────────────────

def test_classify_changes() -> None:
    ch = classify_changes([
        "svc/app/calc.py", "svc/tests/test_calc.py", "svc/tests/data/case.json", "svc/conftest.py",
        "README.md", "deploy/compose.yaml", "web/src/lib/api.ts", "web/src/lib/api.test.ts",
        "web/src/__tests__/x.tsx", "pkg/thing_test.py", "Dockerfile", "web/src/app.css",
    ])
    assert ch["code"] == ["svc/app/calc.py", "web/src/lib/api.ts", "web/src/app.css"]
    assert ch["non_code"] == ["README.md", "deploy/compose.yaml", "Dockerfile"]
    assert set(ch["tests"]) == {"svc/tests/test_calc.py", "svc/tests/data/case.json", "svc/conftest.py",
                                "web/src/lib/api.test.ts", "web/src/__tests__/x.tsx", "pkg/thing_test.py"}


def test_needs_repro() -> None:
    assert evidence.needs_repro(BUG) and evidence.needs_repro(LEGACY)
    assert evidence.needs_repro({"kind": "diff_audit", "narrative": {"finding_type": "security"}})
    assert not evidence.needs_repro(CONTRACT) and not evidence.needs_repro(ARCH)


# ── deciding ─────────────────────────────────────────────────────────────────

def _repro(patched: dict[str, str], base: dict[str, str], notes: list[str] | None = None) -> ReproResult:
    return ReproResult(tool="pytest", patched=patched, base=base, notes=notes or [])


CODE_AND_TEST = ["svc/app/calc.py", "svc/tests/test_calc.py"]


@pytest.mark.parametrize("pack,tool,changed,repro,waiver,status,kind", [
    # bug packs need a test that fails on dev and passes with the fix
    (BUG, "pytest", CODE_AND_TEST, _repro({"t::a": "passed"}, {"t::a": "failed"}), "", "resolved", "repro"),
    (LEGACY, "pytest", CODE_AND_TEST, _repro({"t::a": "passed"}, {"t::a": "failed"}), "", "resolved", "repro"),
    (BUG, "pytest", CODE_AND_TEST, _repro({"t::a": "passed"}, {"t::a": "passed"}), "", "unverified", "none"),
    (BUG, "pytest", CODE_AND_TEST, _repro({"t::a": "passed"}, {"t::a": "error"}), "", "unverified", "none"),
    (BUG, "pytest", CODE_AND_TEST, _repro({}, {}, ["env did not build"]), "", "unverified", "none"),
    (BUG, "pytest", ["svc/app/calc.py"], None, "", "unverified", "none"),            # no test at all
    (BUG, "lint_only", ["svc/app/calc.py"], None, "needs a GPU and a live ComfyUI to exercise", "resolved", "waiver"),
    (BUG, "pytest", ["svc/app/calc.py"], None, "too short", "unverified", "none"),
    # whatever the pack, docs/config-only and test-only changes have their own bar
    (BUG, "lint_only", ["README.md", "deploy/compose.yaml"], None, "", "resolved", "non_code"),
    (BUG, "pytest", ["svc/tests/test_calc.py"], None, "", "resolved", "test_only"),
    (BUG, "lint_only", ["svc/tests/test_calc.py"], None, "", "unverified", "none"),
    # other packs: tests (or npm checks) must have run; lint alone isn't enough
    (CONTRACT, "pytest", ["svc/app/calc.py"], None, "", "resolved", "tests"),
    (ARCH, "pytest+npm_checks", ["svc/app/calc.py"], None, "", "resolved", "tests"),
    (CONTRACT, "lint_only", ["svc/app/calc.py"], None, "", "unverified", "none"),
    (CONTRACT, "node_check", ["web/a.js"], None, "", "unverified", "none"),
])
def test_decide(pack, tool, changed, repro, waiver, status, kind) -> None:
    d = decide(pack, tool=tool, changed=changed, repro=repro, waiver=waiver, base_sha="abc")
    assert (d.status, d.kind) == (status, kind), d.reason
    assert d.evidence["kind"] == kind and d.evidence["base_sha"] == "abc" and d.reason
    if repro is not None:
        assert d.evidence["repro"]["repro"] == repro.repro and "output_sha256" in d.evidence["repro"]


def test_unverified_reasons_say_what_to_do() -> None:
    no_test = decide(BUG, tool="pytest", changed=["svc/app/calc.py"], repro=None)
    assert "add one that fails on dev" in no_test.reason
    passes_on_dev = decide(BUG, tool="pytest", changed=CODE_AND_TEST,
                           repro=_repro({"t::a": "passed"}, {"t::a": "passed"}))
    assert "make a test fail without the fix" in passes_on_dev.reason


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parse_pytest_outcomes() -> None:
    out = ("PASSED tests/test_a.py::test_one\nFAILED tests/test_a.py::test_two - assert 1 == 2\n"
           "ERROR tests/test_b.py - ImportError: cannot import name 'x'\nSKIPPED [1] tests/test_c.py:3: no\n"
           "ERROR tests/test_a.py::test_three - fixture 'db' not found\n")
    assert evidence.parse_pytest_outcomes(out) == {
        "tests/test_a.py::test_one": "passed", "tests/test_a.py::test_two": "failed",
        "tests/test_a.py::test_three": "error"}


def test_parse_vitest_json(tmp_path: Path) -> None:
    data = {"testResults": [{"name": str(tmp_path / "src/a.test.ts"), "assertionResults": [
        {"fullName": "api parses ids", "status": "passed"}, {"fullName": "api rejects", "status": "failed"},
        {"title": "todo", "status": "pending"}]}]}
    assert evidence.parse_vitest_json(data, tmp_path) == {
        "src/a.test.ts::api parses ids": "passed", "src/a.test.ts::api rejects": "failed",
        "src/a.test.ts::todo": "skipped"}


def test_parse_vitest_json_duplicate_names_keep_the_worst_outcome(tmp_path: Path) -> None:
    data = {"testResults": [{"name": str(tmp_path / "a.test.ts"), "assertionResults": [
        {"fullName": "dup", "status": "failed"}, {"fullName": "dup", "status": "passed"},
        {"fullName": "skip", "status": "passed"}, {"fullName": "skip", "status": "pending"},
        {"status": "failed"}, {"status": "passed"}]}]}
    assert evidence.parse_vitest_json(data, tmp_path) == {
        "a.test.ts::dup": "failed", "a.test.ts::skip": "skipped", "a.test.ts::#4": "failed",
        "a.test.ts::#5": "passed"}


def test_copy_into_never_leaves_either_tree(tmp_path: Path) -> None:
    src, dst, host = tmp_path / "src", tmp_path / "out" / "dst", tmp_path / "host"
    for d in (src, dst, host):
        d.mkdir(parents=True)
    (host / "secret.txt").write_text("host secret")
    (host / "victim.txt").write_text("untouched")
    (src / "ok.py").write_text("ok")
    (src / "leak.txt").symlink_to(host / "secret.txt")  # a symlink committed in the audited repo
    (src / "victim.txt").write_text("overwritten")
    (dst / "victim.txt").symlink_to(host / "victim.txt")  # base tree symlink pointing at a host file
    evidence._copy_into(src, dst, ["ok.py", "leak.txt", "victim.txt"])
    assert (dst / "ok.py").read_text() == "ok"
    assert not (dst / "leak.txt").exists()
    assert (host / "victim.txt").read_text() == "untouched"
    evidence._copy_into(src, dst, ["../host/secret.txt"])  # read from tmp/host, would write to tmp/out/host
    assert not (tmp_path / "out" / "host").exists()
    evidence._copy_into(src, dst, [str(host / "secret.txt")])  # absolute: must not touch the host at all
    assert sorted(p.name for p in host.iterdir()) == ["secret.txt", "victim.txt"]


# ── running on real trees ────────────────────────────────────────────────────

BASE_FILES = {
    "svc/requirements.txt": "",
    "svc/app/__init__.py": "",
    "svc/app/calc.py": "def add(a, b):\n    return a - b  # bug\n",
    "svc/tests/test_existing.py": "def test_nothing():\n    assert True\n",
}


def _trees(tmp_path: Path, patched_files: dict[str, str]) -> tuple[Path, Path]:
    base, patched = tmp_path / "base", tmp_path / "patched"
    _write(base, BASE_FILES)
    _write(patched, {**BASE_FILES, **patched_files})
    return base, patched


def _fresh(base: Path, tmp_path: Path):
    """Each call gets its own fresh copy of the dev tree, like the endpoint's repro tree."""
    import itertools
    import shutil

    counter = itertools.count()

    def make() -> Path:
        target = tmp_path / f"repro_base_{next(counter)}"
        shutil.copytree(base, target)
        return target
    return make


def test_fresh_gives_each_call_its_own_clean_tree(tmp_path: Path) -> None:
    base = tmp_path / "dev"
    _write(base, {"a.txt": "dev\n"})
    make = _fresh(base, tmp_path)
    first = make()
    (first / "a.txt").write_text("dirtied\n")
    second = make()
    assert second != first
    assert (second / "a.txt").read_text() == "dev\n"


def test_python_repro_finds_the_failing_then_passing_test(tmp_path: Path, host_python) -> None:
    base, patched = _trees(tmp_path, {
        "svc/app/calc.py": "def add(a, b):\n    return a + b\n",
        "svc/tests/test_calc.py": "from app.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n\n"
                                  "def test_zero():\n    assert add(0, 0) == 0\n",
    })
    res = evidence.python_repro(patched, _fresh(base, tmp_path), ["svc/tests/test_calc.py"], cache_dir=tmp_path / "c")
    assert res.repro == ["svc/tests/test_calc.py::test_add"]  # test_zero passes on dev too: no proof
    assert res.patched["svc/tests/test_calc.py::test_zero"] == "passed" == res.base["svc/tests/test_calc.py::test_zero"]
    assert (base / "svc/tests/test_calc.py").exists() is False  # the shared dev tree is untouched


def test_test_fixture_is_copied_so_dev_fails_only_for_the_bug(tmp_path: Path, host_python) -> None:
    """The implementation is fine on dev; the new test needs its data file. If the data file
    weren't copied, dev would fail and fake a reproduction."""
    base, patched = _trees(tmp_path, {
        "svc/app/calc.py": "def add(a, b):\n    return a + b\n",
        "svc/tests/data/case.json": '{"a": 2, "b": 3, "sum": 5}',
        "svc/tests/test_case.py": "import json, pathlib\nfrom app.calc import add\n\ndef test_case():\n"
                                  "    c = json.loads((pathlib.Path(__file__).parent / 'data/case.json').read_text())\n"
                                  "    assert add(c['a'], c['b']) - c['a'] == c['b'] or True\n",
    })
    tests = ["svc/tests/data/case.json", "svc/tests/test_case.py"]
    res = evidence.python_repro(patched, _fresh(base, tmp_path), tests, cache_dir=tmp_path / "c")
    assert res.base == {"svc/tests/test_case.py::test_case": "passed"} and res.repro == []


def test_test_importing_new_code_proves_nothing(tmp_path: Path, host_python) -> None:
    base, patched = _trees(tmp_path, {
        "svc/app/calc.py": "def add(a, b):\n    return a + b\n\ndef add3(a, b, c):\n    return a + b + c\n",
        "svc/tests/test_add3.py": "from app.calc import add3\n\ndef test_add3():\n    assert add3(1, 2, 3) == 6\n",
    })
    res = evidence.python_repro(patched, _fresh(base, tmp_path), ["svc/tests/test_add3.py"], cache_dir=tmp_path / "c")
    assert res.repro == [] and res.patched == {"svc/tests/test_add3.py::test_add3": "passed"}
    assert any("couldn't be collected on dev" in n for n in res.notes)


def test_node_repro_copies_material_and_compares(tmp_path: Path, monkeypatch) -> None:
    """Orchestration only: vitest itself is faked (installing it needs the network)."""
    pkg = json.dumps({"name": "w", "version": "1.0.0", "scripts": {"test": "vitest run"}})
    lock = json.dumps({"name": "w", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                       "packages": {"": {"name": "w", "version": "1.0.0"}}})
    files = {"web/package.json": pkg, "web/package-lock.json": lock, "web/src/sum.ts": "export const sum = (a, b) => a - b\n"}
    base, patched = tmp_path / "base", tmp_path / "patched"
    _write(base, files)
    _write(patched, {**files, "web/src/sum.ts": "export const sum = (a, b) => a + b\n",
                     "web/src/sum.test.ts": "it('adds')\n", "web/src/__tests__/fixture.json": "{}"})
    monkeypatch.setattr(evidence.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("auditor.validate_node.ensure_node_modules",
                        lambda root, cache: ((tmp_path / "nm").mkdir(exist_ok=True) or tmp_path / "nm", ""))

    def fake_vitest(root: Path, files: list[str], timeout_s: int):
        assert (root / "src/__tests__/fixture.json").exists()  # test material copied to dev too
        fixed = "a + b" in (root / "src/sum.ts").read_text()
        return {"src/sum.test.ts::adds": "passed" if fixed else "failed"}, ""
    monkeypatch.setattr(evidence, "_vitest", fake_vitest)
    res = evidence.node_repro(patched, _fresh(base, tmp_path), ["web/src/sum.test.ts", "web/src/__tests__/fixture.json"],
                              cache_dir=tmp_path / "nc")
    assert res.repro == ["web/src/sum.test.ts::adds"], res.notes


def test_node_repro_needs_vitest(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "p", {"web/package.json": json.dumps({"scripts": {"test": "jest"}}), "web/a.test.ts": "x"})
    monkeypatch.setattr(evidence.shutil, "which", lambda name: "/usr/bin/" + name)
    res = evidence.node_repro(tmp_path / "p", lambda: None, ["web/a.test.ts"], cache_dir=tmp_path / "nc")
    assert not res.ran and "isn't vitest" in res.notes[0]


# ── the endpoint ─────────────────────────────────────────────────────────────

@pytest.fixture
def mono(tmp_path: Path, monkeypatch, host_python):
    """A git repo with a bug, an ecosystem config pointing at it, and a bug pack on calc.py."""
    from auditor.agent_pack import emit_packs_from_findings
    from auditor.contracts import Finding

    repo = tmp_path / "mono"
    _write(repo, BASE_FILES)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")

    class _RepoCfg:
        name = "mono"
        path = str(repo)

    class _Cfg:
        output_dir = str(tmp_path / "reports")
        auth_mode = False
        auth_code_env = "AI_AUDIT_AUTH_CODE"
        repos = [_RepoCfg()]

    monkeypatch.setattr("auditor.audit_api.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: _Cfg.output_dir)
    monkeypatch.setattr("auditor.repo_resolver.resolve_repo_path", lambda r: Path(r.path))
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")
    monkeypatch.delenv("AI_AUDIT_API_TOKEN", raising=False)
    monkeypatch.delenv("AI_AUDIT_EVIDENCE_BAR", raising=False)
    f = Finding(id="llm:calc:1", type="bug", severity="HIGH", repo="mono", file_path=str(repo / "svc/app/calc.py"),
                line=2, title="add subtracts", description="d", evidence="e", recommendation="r", source="llm")
    [pack] = emit_packs_from_findings([f], output_dir=_Cfg.output_dir, kind="nightly", run_id="run-1")
    return {"out": _Cfg.output_dir, "pack": pack.work_item_id}


FIX = ("--- a/svc/app/calc.py\n+++ b/svc/app/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
       "-    return a - b  # bug\n+    return a + b\n")
NEW_TEST = ("--- /dev/null\n+++ b/svc/tests/test_calc.py\n@@ -0,0 +1,5 @@\n+from app.calc import add\n+\n+\n"
            "+def test_add():\n+    assert add(2, 3) == 5\n")


def _post(**body):
    resp = TestClient(app).post("/audit/validate_patch", json={"repo": "mono", **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_endpoint_resolves_with_a_reproducing_test(mono) -> None:
    from auditor.agent_pack import load_pack

    assert load_pack(mono["out"], mono["pack"])["narrative"]["finding_type"] == "bug"
    body = _post(patch=FIX + NEW_TEST, pack_id=mono["pack"])
    assert body["passed"] and body["tool"] == "pytest", body
    assert body["pack"]["status"] == "resolved" and body["pack"]["evidence"] == "repro", body["pack"]
    assert body["evidence"]["repro"]["repro"] == ["svc/tests/test_calc.py::test_add"]
    data = load_pack(mono["out"], mono["pack"])
    assert data["status"] == "resolved" and "evidence=repro" in data["status_note"]
    ev = data["verification"]["evidence"]
    assert ev["kind"] == "repro" and ev["base_sha"] and ev["at"] and ev["repro"]["base"] == {
        "svc/tests/test_calc.py::test_add": "failed"}


def test_endpoint_marks_unverified_without_a_test_and_resolves_with_a_waiver(mono) -> None:
    from auditor.agent_pack import list_packs, load_pack

    body = _post(patch=FIX, pack_id=mono["pack"])
    assert body["passed"] and body["pack"]["status"] == "unverified", body
    assert "add one that fails on dev" in body["pack"]["reason"]
    data = load_pack(mono["out"], mono["pack"])
    assert data["status"] == "unverified" and data["status_note"].startswith("validate_patch passed, unverified")
    # unverified is still open work: the default bundle filter includes it
    assert mono["pack"] in {e["id"] for e in list_packs(mono["out"], status="open,claimed,in_progress,unverified")}

    waived = _post(patch=FIX, pack_id=mono["pack"],
                   evidence_waiver="add() is exercised only by the GPU pipeline; no unit seam exists")
    assert waived["pack"]["status"] == "resolved" and waived["pack"]["evidence"] == "waiver"
    assert "GPU pipeline" in load_pack(mono["out"], mono["pack"])["verification"]["evidence"]["waiver"]


def test_endpoint_test_that_passes_on_dev_is_not_proof(mono) -> None:
    passes_anyway = ("--- /dev/null\n+++ b/svc/tests/test_calc.py\n@@ -0,0 +1,5 @@\n+from app.calc import add\n+\n+\n"
                     "+def test_add_zero():\n+    assert add(0, 0) == 0\n")
    body = _post(patch=FIX + passes_anyway, pack_id=mono["pack"])
    assert body["pack"]["status"] == "unverified" and "make a test fail" in body["pack"]["reason"]


def test_evidence_bar_off_restores_old_rule(mono, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_EVIDENCE_BAR", "0")
    body = _post(patch=FIX, pack_id=mono["pack"])
    assert body["pack"]["status"] == "resolved" and "evidence" not in body


def test_trust_metrics_count_evidence_kinds() -> None:
    from auditor import trust_metrics as tm

    def p(pid: str, note: str) -> tm.PackView:
        return tm.pack_view({"work_item_id": pid, "kind": "nightly", "status": "resolved", "repo": {"name": "x"},
                             "status_history": [{"status": "resolved", "at": "2026-11-16T00:00:00+00:00", "note": note}]})
    m = tm.false_resolve_metric(tm.resolution_events([
        p("a", "validate_patch passed (repo=x, tool=pytest, evidence=repro)"),
        p("b", "validate_patch passed (repo=x, tool=lint_only, evidence=waiver)"),
        p("c", "validate_patch passed (repo=x, tool=lint_only)"),  # before the bar
    ]))
    assert m["by_evidence"] == {"repro": 1, "waiver": 1} and m["waiver_rate"] == 0.5
    assert m["lint_only"] == 2  # a waived lint-only resolution still counts as lint-only


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_endpoint_validates_a_merged_fix_at_an_older_base(mono) -> None:
    """#91: once the fix is on dev the patch no longer applies at the tip; base_ref pins the
    commit before it, the patch applies there, and the "before" run uses that commit too."""
    from auditor.agent_pack import load_pack

    repo = Path(mono["out"]).parent / "mono"
    before = _head(repo)
    (repo / "svc/app/calc.py").write_text((repo / "svc/app/calc.py").read_text().replace("a - b  # bug", "a + b"))
    _git(repo, "commit", "-qam", "fix merged")

    at_tip = _post(patch=FIX + NEW_TEST, pack_id=mono["pack"])
    assert not at_tip["patch_applied"] and at_tip["skipped_reason"] == "patch did not apply"

    body = _post(patch=FIX + NEW_TEST, pack_id=mono["pack"], base_ref=before)
    assert body["patch_applied"] and body["passed"], body
    assert body["base_ref"] == before and body["base_sha"] == before and body["base_source"] == "pinned"
    assert body["pack"]["status"] == "resolved" and body["pack"]["evidence"] == "repro", body["pack"]
    ev = load_pack(mono["out"], mono["pack"])["verification"]["evidence"]
    assert ev["base_sha"] == before and ev["base_ref"] == before
    assert ev["repro"]["base"] == {"svc/tests/test_calc.py::test_add": "failed"}


def test_endpoint_refuses_a_base_ref_off_dev(mono) -> None:
    repo = Path(mono["out"]).parent / "mono"
    _git(repo, "checkout", "-qb", "side")
    (repo / "svc/app/side.py").write_text("X = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "side")
    side = _head(repo)
    _git(repo, "checkout", "-q", "-")
    client = TestClient(app)
    for ref, why in ((side, "not an ancestor"), ("--output=/tmp/x", "invalid git ref"), ("nope", "not a commit")):
        resp = client.post("/audit/validate_patch", json={"repo": "mono", "patch": FIX, "base_ref": ref})
        assert resp.status_code == 400 and why in resp.json()["detail"], (ref, resp.text)


# ── revalidation (ai-audit revalidate) ───────────────────────────────────────

def _history(out, pid):
    from auditor.agent_pack import load_pack

    d = load_pack(out, pid)
    return d["status"], d["verification"]["evidence"]["kind"], [h["status"] for h in d["status_history"]]


WAIVER = "add() is exercised only by the GPU pipeline; no unit seam exists"


def test_revalidate_upgrades_a_waived_resolution_and_the_metric_reads_it(mono) -> None:
    from auditor import trust_metrics as tm

    _post(patch=FIX, pack_id=mono["pack"], evidence_waiver=WAIVER)
    body = _post(patch=FIX + NEW_TEST, pack_id=mono["pack"], revalidate=True)
    assert body["pack"]["updated"] and body["pack"]["evidence"] == "repro", body["pack"]
    status, kind, hist = _history(mono["out"], mono["pack"])
    assert (status, kind) == ("resolved", "repro") and hist[-2:] == ["resolved", "resolved"]
    [e] = tm.resolution_events(tm.load_packs(mono["out"]))  # still one resolution, now proven
    assert (e["method"], e["evidence"]) == ("validator:pytest", "repro")


def test_revalidate_never_demotes(mono) -> None:
    _post(patch=FIX, pack_id=mono["pack"], evidence_waiver=WAIVER)
    body = _post(patch=FIX, pack_id=mono["pack"], revalidate=True)  # no test: nothing stronger
    assert not body["pack"]["updated"] and "left as it was" in body["pack"]["reason"]
    assert _history(mono["out"], mono["pack"])[:2] == ("resolved", "waiver")


def test_revalidate_leaves_a_proven_resolution_alone(mono) -> None:
    _post(patch=FIX + NEW_TEST, pack_id=mono["pack"])
    body = _post(patch=FIX + NEW_TEST, pack_id=mono["pack"], revalidate=True)
    assert not body["pack"]["updated"] and "nothing to revalidate" in body["pack"]["reason"]


def test_revalidate_resolves_an_unverified_pack_with_proof(mono) -> None:
    assert _post(patch=FIX, pack_id=mono["pack"])["pack"]["status"] == "unverified"
    body = _post(patch=FIX + NEW_TEST, pack_id=mono["pack"], revalidate=True)
    assert body["pack"]["updated"] and _history(mono["out"], mono["pack"])[:2] == ("resolved", "repro")


def test_revalidate_does_nothing_with_the_bar_off(mono, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_EVIDENCE_BAR", "0")
    body = _post(patch=FIX + NEW_TEST, pack_id=mono["pack"], revalidate=True)
    assert not body["pack"]["updated"] and _history_status(mono["out"], mono["pack"]) == "open"


def _history_status(out, pid):
    from auditor.agent_pack import load_pack

    return load_pack(out, pid)["status"]


def test_loose_tests_repro_per_directory_so_one_collection_error_spares_the_rest(tmp_path: Path, host_python) -> None:
    # argus-ci#23 (#102): no project marker; one test dir imports a module the env lacks, which
    # used to abort the single pytest run, so no test produced a result and the pack stayed unverified.
    loose = {".git/HEAD": "", "rules/check.py": "def ok(x):\n    return False\n",
             "scripts/test_wf.py": "import not_a_real_module_xyz\n\ndef test_wf():\n    pass\n"}
    base, patched = tmp_path / "base", tmp_path / "patched"
    _write(base, loose)
    _write(patched, {**loose, "rules/check.py": "def ok(x):\n    return True\n",
                     "rules/test_check.py": ("import sys\nfrom pathlib import Path\n"
                                             "sys.path.insert(0, str(Path(__file__).parent))\nimport check\n\n"
                                             "def test_ok():\n    assert check.ok(1)\n")})
    tests = ["rules/test_check.py", "scripts/test_wf.py"]
    res = evidence.python_repro(patched, _fresh(base, tmp_path), tests, cache_dir=tmp_path / "c")
    assert res.repro == ["rules/test_check.py::test_ok"]
    assert any(n.startswith("scripts: the patch's tests produced no results") for n in res.notes)


def _second_bug_pack(mono) -> str:
    from auditor.agent_pack import emit_packs_from_findings
    from auditor.contracts import Finding

    repo = Path(mono["out"]).parent / "mono"
    f = Finding(id="llm:calc:2", type="bug", severity="MEDIUM", repo="mono", file_path=str(repo / "svc/app/calc.py"),
                line=2, title="sum of positives comes out negative", description="d", evidence="e",
                recommendation="r", source="llm")
    [pack] = emit_packs_from_findings([f], output_dir=mono["out"], kind="nightly", run_id="run-2")
    assert pack.work_item_id != mono["pack"]
    return pack.work_item_id


def test_one_validation_resolves_every_listed_pack_with_one_repro_run(mono, monkeypatch) -> None:
    from auditor import evidence
    from auditor.agent_pack import load_pack

    other = _second_bug_pack(mono)
    calls = []
    real = evidence.run_repro
    monkeypatch.setattr(evidence, "run_repro", lambda *a, **k: calls.append(1) or real(*a, **k))
    body = _post(patch=FIX + NEW_TEST, pack_id=mono["pack"], pack_ids=[other, mono["pack"], "pack-missing"])
    assert body["passed"] and body["pack"]["id"] == mono["pack"] and body["pack"]["status"] == "resolved"
    # The raw list, not a dict keyed by id: a dict would collapse a repeated entry and hide it
    assert [p["id"] for p in body["packs"]] == [mono["pack"], other, "pack-missing"]  # pack_id first, repeats dropped
    by_id = {p["id"]: p for p in body["packs"]}
    for pid in (mono["pack"], other):
        assert by_id[pid]["status"] == "resolved" and by_id[pid]["evidence"] == "repro", by_id[pid]
        assert by_id[pid]["evidence_record"]["repro"]["repro"] == ["svc/tests/test_calc.py::test_add"]
        assert load_pack(mono["out"], pid)["status"] == "resolved"
    assert by_id["pack-missing"] == {"id": "pack-missing", "updated": False, "reason": "pack not found"}
    assert len(calls) == 1  # the repro ran once for both packs


def test_each_listed_pack_is_judged_on_its_own_evidence(mono) -> None:
    from auditor.agent_pack import load_pack

    other = _second_bug_pack(mono)
    body = _post(patch=FIX, pack_ids=[mono["pack"], other])  # no test: neither has proof
    assert [p["status"] for p in body["packs"]] == ["unverified", "unverified"]
    assert load_pack(mono["out"], other)["status"] == "unverified"


def test_a_failed_validation_touches_none_of_the_listed_packs(mono) -> None:
    from auditor.agent_pack import load_pack

    other = _second_bug_pack(mono)
    broken = ("--- a/svc/app/calc.py\n+++ b/svc/app/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
              "-    return a - b  # bug\n+    return a +\n")
    body = _post(patch=broken + NEW_TEST, pack_ids=[mono["pack"], other])
    assert not body["passed"]
    assert all(p["updated"] is False and p["reason"] == "validation did not pass" for p in body["packs"])
    assert {load_pack(mono["out"], p)["status"] for p in (mono["pack"], other)} == {"open"}


def test_pack_ids_are_capped(monkeypatch) -> None:
    from auditor.audit_api import MAX_PACKS_PER_VALIDATION

    monkeypatch.delenv("AI_AUDIT_API_TOKEN", raising=False)

    resp = TestClient(app).post("/audit/validate_patch", json={
        "repo": "mono", "patch": "", "pack_ids": [f"p{i}" for i in range(MAX_PACKS_PER_VALIDATION + 1)]})
    assert resp.status_code == 422


# ── #127: in a multi-pack request each pack is judged on its own tests ───────

def _pack_at(path: str) -> dict:
    return {"location": {"file_path": path}, "narrative": {"finding_type": "bug"}, "kind": "nightly"}


def test_for_pack_keeps_only_the_tests_naming_the_packs_module(tmp_path: Path) -> None:
    _write(tmp_path, {
        "tests/test_multi.py": "from app.repair.multi_model import validate\n\ndef test_a():\n    pass\n",
        "tests/test_lease.py": "from app import gpu_lease\n\ndef test_b():\n    pass\n",
        "tests/test_models.py": "from app.repair import models\n\ndef test_c():\n    pass\n",
    })
    ids = ["tests/test_multi.py::test_a", "tests/test_lease.py::test_b", "tests/test_models.py::test_c"]
    repro = evidence.ReproResult(tool="pytest", patched=dict.fromkeys(ids, "passed"), base=dict.fromkeys(ids, "failed"))
    multi = evidence.for_pack(repro, _pack_at("/m/rag-chat/app/repair/multi_model.py"), tmp_path)
    assert multi.repro == ["tests/test_multi.py::test_a"]
    assert multi.scoped_out == ["tests/test_lease.py::test_b", "tests/test_models.py::test_c"]
    assert evidence.for_pack(repro, _pack_at("rag-chat/app/gpu_lease.py"), tmp_path).repro == ["tests/test_lease.py::test_b"]
    # a generic stem ties only with its parent directory: "models" alone would match anything
    assert evidence.for_pack(repro, _pack_at("app/repair/models.py"), tmp_path).repro == ["tests/test_models.py::test_c"]
    assert evidence.for_pack(repro, _pack_at("other/models.py"), tmp_path).repro == []
    # the caller can name a pack's tests, by id or file, and that's all it gets
    named = evidence.for_pack(repro, _pack_at("app/repair/multi_model.py"), tmp_path, named=["tests/test_lease.py"])
    assert named.repro == ["tests/test_lease.py::test_b"] and "tests/test_multi.py::test_a" in named.scoped_out
    # a test id pointing outside the tree is never read
    out = evidence.ReproResult(patched={"../../etc/passwd::t": "passed"}, base={"../../etc/passwd::t": "failed"})
    assert evidence.for_pack(out, _pack_at("app/passwd.py"), tmp_path).repro == []


def test_decide_names_tests_that_belong_to_other_packs() -> None:
    scoped = evidence.ReproResult(tool="pytest", scoped_out=["tests/test_lease.py::test_b"])
    d = evidence.decide(_pack_at("app/x.py"), tool="pytest", changed=["app/x.py", "tests/test_lease.py"], repro=scoped)
    assert d.status == "unverified" and "belong to other packs" in d.reason


def _bug_pack_in(mono, rel: str, title: str) -> str:
    from auditor.agent_pack import emit_packs_from_findings
    from auditor.contracts import Finding

    repo = Path(mono["out"]).parent / "mono"
    f = Finding(id=f"llm:{rel}", type="bug", severity="MEDIUM", repo="mono", file_path=str(repo / rel),
                line=1, title=title, description="d", evidence="e", recommendation="r", source="llm")
    [pack] = emit_packs_from_findings([f], output_dir=mono["out"], kind="nightly", run_id="run-3")
    return pack.work_item_id


def test_a_pack_never_resolves_on_another_packs_test(mono) -> None:
    from auditor.agent_pack import load_pack

    other = _bug_pack_in(mono, "svc/app/lease_vetting.py", "lease_vetting lets a metadata address through")
    body = _post(patch=FIX + NEW_TEST, pack_ids=[mono["pack"], other])
    by_id = {p["id"]: p for p in body["packs"]}
    assert by_id[mono["pack"]]["status"] == "resolved" and by_id[mono["pack"]]["evidence"] == "repro"
    assert by_id[other]["status"] == "unverified" and "belong to other packs" in by_id[other]["reason"], by_id[other]
    assert by_id[other]["evidence_record"]["repro"]["scoped_out"] == ["svc/tests/test_calc.py::test_add"]
    assert load_pack(mono["out"], other)["status"] == "unverified"
    # a lone pack is still judged on the whole patch, as before
    alone = _post(patch=FIX + NEW_TEST, pack_id=other)
    assert alone["pack"]["status"] == "resolved"


def test_pack_tests_names_a_packs_evidence_and_refuses_strangers(mono) -> None:
    other = _bug_pack_in(mono, "svc/app/lease_vetting.py", "lease_vetting lets a metadata address through")
    body = _post(patch=FIX + NEW_TEST, pack_ids=[mono["pack"], other],
                 pack_tests={other: ["svc/tests/test_calc.py::test_add"]})
    assert [p["status"] for p in body["packs"]] == ["resolved", "resolved"]
    resp = TestClient(app).post("/audit/validate_patch", json={
        "repo": "mono", "patch": FIX, "pack_ids": [mono["pack"]], "pack_tests": {"pack-typo": ["t.py"]}})
    assert resp.status_code == 422 and "pack-typo" in resp.text
