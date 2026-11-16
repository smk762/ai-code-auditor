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
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache_dir: (Path(sys.executable), ""))


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
    """Each call site gets its own copy of the dev tree, like the endpoint's repro tree."""
    import shutil

    target = tmp_path / "repro_base"

    def make() -> Path:
        if not target.exists():
            shutil.copytree(base, target)
        return target
    return make


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
