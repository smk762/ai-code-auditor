"""Evidence for structural packs (#58): resolved only when the violation is really gone."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import agent_pack, evidence, pack_classes, revalidate, structure, trust_metrics, validator
from auditor.agent_pack import AgentCodingPack, load_pack, write_pack
from auditor.audit_api import app

BASE = {
    "app/__init__.py": "",
    "app/a.py": "from app.b import LIMIT\n\n\ndef run():\n    return LIMIT\n",
    "app/b.py": "LIMIT = 5\n\n\ndef go():\n    from app.a import run\n    return run()\n",
    "app/c.py": "x = 1\n",
}
CYCLE = {"kind": "module_cycle", "governance": {"structure": {"members": ["app/a.py", "app/b.py"]}}}
PRIV_BASE = {"app/__init__.py": "", "app/core.py": "_x = 1\n", "app/user.py": "from app.core import _x\n"}
PRIVATE = {"kind": "private_import",
           "governance": {"structure": {"path": "app/user.py", "defined_in": "app/core.py", "name": "_x"}}}
LIMITS = {"app/limits.py": "LIMIT = 5\n"}


def _tree(base: dict[str, str], patch: dict[str, str | None]) -> dict[str, str]:
    return {k: v for k, v in {**base, **patch}.items() if v is not None}


def _judge(pack: dict, base: dict[str, str], patch: dict[str, str | None]) -> dict:
    return structure.judge(pack, structure.scan_sources(base), structure.scan_sources(_tree(base, patch)))


@pytest.mark.parametrize("patch", [
    {**LIMITS, "app/a.py": "from app.limits import LIMIT\n\n\ndef run():\n    return LIMIT\n"},
    {"app/a.py": None, "app/b.py": "LIMIT = 5\n\n\ndef run():\n    return LIMIT\n"},  # one module
], ids=["moved", "merged"])
def test_a_real_fix_is_gone(patch) -> None:
    assert _judge(CYCLE, BASE, patch)["gone"] is True


@pytest.mark.parametrize("patch", [
    {},
    {"app/a.py": "def run():\n    from app.b import LIMIT\n    return LIMIT\n"},
    {"app/__init__.py": "from app.b import LIMIT\n", "app/a.py": "from app import LIMIT\n\n\ndef run():\n    return LIMIT\n"},
    {"app/relay.py": "from app.b import LIMIT\n", "app/a.py": "from app.relay import LIMIT\nrun = LIMIT\n"},
    # one member renamed, then both: the pack's paths leave the cycle, the cycle stays
    {"app/b.py": None, "app/b2.py": BASE["app/b.py"], "app/a.py": BASE["app/a.py"].replace("app.b", "app.b2")},
    {"app/a.py": None, "app/b.py": None, "app/a2.py": BASE["app/a.py"].replace("app.b", "app.b2"),
     "app/b2.py": BASE["app/b.py"].replace("app.a", "app.a2")},
    # the cycle is broken here and made somewhere else
    {"app/a.py": "run = 1\n", "app/c.py": "from app.d import y\n", "app/d.py": "y = 1\nfrom app import c\n"},
], ids=["nothing", "lazy", "re_export", "relay", "renamed_one", "renamed_both", "elsewhere"])
def test_a_dodge_is_not_gone(patch) -> None:
    assert _judge(CYCLE, BASE, patch)["gone"] is False


def test_part_of_a_big_cycle_left_is_not_gone() -> None:
    ring = {f"app/{a}.py": f"from app import {b}\n" for a, b in (("a", "b"), ("b", "c"), ("c", "a"))}
    ring["app/__init__.py"] = ""
    pack = {"kind": "module_cycle", "governance": {"structure": {"members": ["app/a.py", "app/b.py", "app/c.py"]}}}
    left = _judge(pack, ring, {"app/c.py": "from app import b\n"})
    assert left["gone"] is False and "app/b.py, app/c.py" in left["reason"]


@pytest.mark.parametrize("patch,gone", [
    ({"app/core.py": "x = 1\n", "app/user.py": "from app.core import x\n"}, True),
    ({"app/user.py": "def f():\n    from app import core\n    return core._x\n"}, False),
    ({"app/user.py": None, "app/user2.py": PRIV_BASE["app/user.py"]}, False),
    ({"app/core.py": "_y = 1\n", "app/user.py": "from app.core import _y\n"}, False),
    ({"app/relay.py": "from app.core import _x\nx = _x\n", "app/user.py": "from app.relay import x\n"}, False),
    ({"app/core.py": "def x():\n    from app import user\n", "app/user.py": "from app.core import x\n"}, False),
], ids=["public", "attribute", "moved", "renamed", "relay", "made_a_cycle"])
def test_private_import_verdicts(patch, gone) -> None:
    assert _judge(PRIVATE, PRIV_BASE, patch)["gone"] is gone


def test_unknown_is_never_gone() -> None:
    ok = structure.scan_sources(BASE)
    fixed = structure.scan_sources({**BASE, "app/a.py": "run = 1\n"})
    assert structure.judge(CYCLE, ok, fixed)["gone"] is True  # what the rest would be without the problem
    broken = structure.scan_sources({**BASE, "app/a.py": "run = 1\n", "app/c.py": "def (:\n"})
    for base, patched in ((ok, broken), (ok, {"error": "x", "complete": False}), ({"error": "x"}, fixed), (fixed, fixed)):
        assert structure.judge(CYCLE, base, patched)["gone"] is False
    for bad in ({"kind": "module_cycle"}, {"kind": "private_import"}, {"kind": "nightly", "governance": CYCLE["governance"]}):
        assert structure.judge(bad, ok, fixed)["gone"] is False


def test_a_crashed_or_timed_out_scan_is_an_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_AUDIT_SCAN_PYTHON", raising=False)
    monkeypatch.setattr(sys, "executable", "/bin/false")
    assert structure.scan_tree_isolated(tmp_path)["error"]
    monkeypatch.undo()
    assert structure.scan_tree_isolated(tmp_path, timeout_s=0)["error"]


def test_scan_tree_matches_and_never_follows_symlinks(tmp_path: Path) -> None:
    _write(tmp_path / "t", BASE)
    (tmp_path / "out.py").write_text("def (:\n")
    (tmp_path / "t/app/link.py").symlink_to(tmp_path / "out.py")
    assert structure.scan_tree_isolated(tmp_path / "t") == structure.scan_sources(BASE)


CODE = ["app/a.py"]
WAIVER = "no test can exercise this without the real GPU"


@pytest.mark.parametrize("verdict,tool,changed,waiver,status,kind", [
    ({"gone": True, "reason": "r"}, "pytest", CODE, "", "resolved", "structure"),
    ({"gone": True, "reason": "r"}, "lint_only", CODE, "", "unverified", "none"),
    ({"gone": True, "reason": "r"}, "lint_only", CODE, WAIVER, "resolved", "waiver"),
    ({"gone": False, "reason": "r"}, "pytest", CODE, "", "unverified", "none"),
    ({"gone": False, "reason": "r"}, "pytest", CODE, WAIVER, "unverified", "none"),
    ({"gone": False, "reason": "r"}, "pytest", ["README.md"], "", "unverified", "none"),
    ({"gone": False, "reason": "r"}, "pytest", ["tests/test_a.py"], "", "unverified", "none"),
    (None, "pytest", CODE, WAIVER, "unverified", "none"),
])
def test_no_shortcut_or_waiver_resolves_a_cycle_that_is_still_there(verdict, tool, changed, waiver, status, kind) -> None:
    d = evidence.decide(CYCLE, tool=tool, changed=changed, repro=None, waiver=waiver, structure=verdict)
    assert (d.status, d.kind) == (status, kind), d.reason


def test_structural_kinds_agree_everywhere() -> None:
    kinds = evidence.STRUCTURE_KINDS
    assert kinds == agent_pack.STRUCTURE_KINDS
    for excluded in (evidence.FINDING_PACK_EXCLUDE, revalidate.FINDING_KINDS_EXCLUDED, trust_metrics.NON_FINDING_KINDS,
                     pack_classes.NON_FINDING_KINDS, agent_pack._NO_TITLE_MATCH_KINDS):
        assert kinds <= excluded


# ── brief and endpoint ───────────────────────────────────────────────────────

def _pack(kind: str = "module_cycle") -> AgentCodingPack:
    return AgentCodingPack(work_item_id="pack-cycle", kind=kind, priority={}, repo={"name": "mono"},
                           location={"file_path": "app/a.py"}, narrative={"title": "cycle"},
                           governance=dict(CYCLE["governance"]))


def test_the_structural_brief_asks_for_design_not_a_minimal_patch() -> None:
    brief = _pack().to_brief_md()
    assert "minimal patches" not in brief and "relay module" in brief and "before/after diagram" in brief
    assert "never for the structure" in brief and "violation is gone" in _pack().to_compact_md()
    assert "Prefer minimal patches" in _pack("nightly").to_brief_md()


def _write(root: Path, files: dict[str, str]) -> None:
    for p, t in files.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text(t)


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


TEST = "from app.b import go\n\n\ndef test_go():\n    assert go() == 5\n"


@pytest.fixture
def mono(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache_dir, extra=None: (Path(sys.executable), ""))
    repo = tmp_path / "mono"
    _write(repo, {**BASE, "pyproject.toml": "[project]\nname='m'\nversion='0'\n", "tests/test_go.py": TEST})
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
    write_pack(_pack(), _Cfg.output_dir)

    def post(files: dict[str, str]) -> dict:
        _write(repo, files)
        _git(repo, "add", "-AN", ".")
        patch = _git(repo, "diff")
        _git(repo, "reset", "-q", "--hard")
        _git(repo, "clean", "-qfd")
        r = TestClient(app).post("/audit/validate_patch", json={"repo": "mono", "patch": patch, "pack_id": "pack-cycle"})
        assert r.status_code == 200, r.text
        assert r.json()["passed"], r.json()
        return r.json()["pack"]
    return post, _Cfg.output_dir


FIXED_A = "from app.limits import LIMIT\n\n\ndef run():\n    return LIMIT\n"


def test_endpoint_resolves_a_real_fix_and_records_the_verdict(mono) -> None:
    post, out = mono
    assert post({**LIMITS, "app/a.py": FIXED_A})["status"] == "resolved"
    ev = load_pack(out, "pack-cycle")["verification"]["evidence"]
    assert ev["kind"] == "structure" and ev["structure"]["gone"] is True


def test_endpoint_leaves_a_dodge_unverified(mono) -> None:
    post, _ = mono
    p = post({"app/a.py": "def run():\n    from app.b import LIMIT\n    return LIMIT\n"})
    assert p["status"] == "unverified" and "still in a cycle" in p["reason"]


def test_a_patchs_tests_cannot_rewrite_the_tree_that_is_judged(mono) -> None:
    post, _ = mono
    evil = TEST + ("\n\ndef test_evil():\n    import pathlib\n\n"
                   "    root = pathlib.Path(__file__).resolve().parents[1]\n"
                   "    (root / 'app/a.py').write_text('run = 1\\n')\n")
    p = post({"app/a.py": BASE["app/a.py"] + "\n\nY = 1\n", "tests/test_go.py": evil})
    assert p["status"] == "unverified" and "still in a cycle" in p["reason"]
