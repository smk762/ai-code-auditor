"""Seeded recall of class guards (#63): plant known instances of a guarded class, see the guard catch them."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from auditor import guard_seeded as gs
from auditor import validator
from auditor.trust_eval import build_gates

SEEDS = """\
silent_failure:
  - id: t_except_pass
    code: |
      def _seeded_t_except_pass(path):
          try:
              open(path).read()
          except Exception:
              pass
  - id: t_except_continue
    imports: [import json]
    code: |
      def _seeded_t_except_continue(lines):
          out = []
          for line in lines:
              try:
                  out.append(json.loads(line))
              except Exception:
                  continue
          return out
"""

# A test-based guard: scans pkg/ for a broad except whose whole body is one of KINDS.
PROOF = """\
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
KINDS = {kinds}


def hits(path):
    out = []
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.ExceptHandler) and len(n.body) == 1 and isinstance(n.body[0], KINDS):
            out.append(n.lineno)
    return out


def test_fires_on_the_fixture():
    assert hits(FIX / "bad.py")


def test_repo_is_clean():
    files = sorted((REPO / "pkg").rglob("*.py"))
    assert files
    assert [h for p in files for h in hits(p)] == []
"""

CORE = '"""Core."""\nimport os\n\n\ndef home():\n    return os.environ.get("HOME", "")\n'
BAD = "try:\n    x = 1\nexcept Exception:\n    pass\n"


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _mirror(tmp: Path, *, kinds: str = "(ast.Pass, ast.Continue)", core: str = CORE) -> tuple[Path, str]:
    repo = tmp / "mirror" / "demo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg/__init__.py").write_text("")
    (repo / "pkg/core.py").write_text(core)
    (repo / "pkg/util.py").write_text("import sys\n\n\ndef argv():\n    return sys.argv\n")
    (repo / "tests/fixtures").mkdir(parents=True)
    (repo / "tests/fixtures/bad.py").write_text(BAD)
    (repo / "tests/test_guard.py").write_text(PROOF.format(kinds=kinds))
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "guard")
    return repo, _git(repo, "rev-parse", "HEAD")


def _setup(tmp: Path, monkeypatch, *, proof: str = "proven", rule: dict | None = None, **kw) -> tuple[Path, Path, Path]:
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (Path(sys.executable), ""))
    repo, sha = _mirror(tmp, **kw)
    mirrors = tmp / "mirrors.yaml"
    mirrors.write_text(f"mirrors:\n  - name: demo\n    path: {repo}\n")
    seeds = tmp / "seeds.yaml"
    seeds.write_text(SEEDS)
    out = tmp / "reports"
    (out / "agent_packs").mkdir(parents=True)
    rec = {"commit": sha, "proof_test": "tests/test_guard.py", "fixtures": ["tests/fixtures/bad.py"],
           "proof": {"status": proof}}
    if rule:
        rec["rule"] = rule
    (out / "agent_packs/pack-class-silent-failure.yaml").write_text(yaml.safe_dump({
        "work_item_id": "pack-class-silent-failure", "kind": "class_guard", "status": "in_progress",
        "governance": {"class_id": "silent_failure", "class_guard": {"demo": rec}}}))
    return out, mirrors, seeds


def _run(out: Path, mirrors: Path, seeds: Path, **kw) -> dict:
    return gs.run(out, mirrors_path=mirrors, seeds_path=seeds, seed="fixed", **kw)


def _gate(result: dict):
    return {g.id: g for g in build_gates(None, None, guard_seeded=result)}["guards.seeded_recall"]


def test_a_guard_that_catches_every_seed_passes(tmp_path: Path, monkeypatch) -> None:
    out, mirrors, seeds = _setup(tmp_path, monkeypatch)
    r = _run(out, mirrors, seeds)
    g = r["guards"][0]
    assert g["status"] == "ok" and g["scope"] == "traced", g
    assert {s["seed"]: s["status"] for s in g["seeds"]} == {"t_except_pass": "caught", "t_except_continue": "caught"}
    assert all(s["file"].startswith("pkg/") for s in g["seeds"])  # traced scope: pkg/, never the fixture
    assert all("tests/test_guard.py::test_repo_is_clean" in s["caught_by"] for s in g["seeds"])
    assert r["summary"] == {"guards": 1, "planted": 2, "caught": 2, "missed": 0, "invalid": 0}
    saved = json.loads((out / gs.RESULT_NAME).read_text())
    gate = _gate(saved)
    assert gate.status == "pass" and gate.value == 1.0 and gate.n == 2 and gate.min_n == 2


def test_a_guard_that_misses_a_seed_fails_the_gate(tmp_path: Path, monkeypatch) -> None:
    out, mirrors, seeds = _setup(tmp_path, monkeypatch, kinds="(ast.Pass,)")
    r = _run(out, mirrors, seeds)
    by = {s["seed"]: s for s in r["guards"][0]["seeds"]}
    assert by["t_except_pass"]["status"] == "caught"
    miss = by["t_except_continue"]
    assert miss["status"] == "missed" and miss["test"] == "missed" and "continue" in miss["code"]
    gate = _gate(r)
    assert gate.status == "fail" and gate.value == 0.5 and "t_except_continue" in gate.note


def test_a_check_that_already_fails_unplanted_is_invalid_not_a_miss(tmp_path: Path, monkeypatch) -> None:
    dirty = CORE + "\n\ndef f():\n    try:\n        home()\n    except Exception:\n        pass\n"
    out, mirrors, seeds = _setup(tmp_path, monkeypatch, core=dirty)
    r = _run(out, mirrors, seeds)
    g = r["guards"][0]
    assert g["status"] == "invalid" and "already fails" in g["note"] and g["seeds"] == []
    assert r["summary"]["missed"] == 0
    gate = _gate(r)  # a proven guard with no valid seed can't drop out of the denominator
    assert gate.status == "insufficient" and "too few valid seeds" in gate.note


def test_no_proven_guards_is_insufficient(tmp_path: Path, monkeypatch) -> None:
    out, mirrors, seeds = _setup(tmp_path, monkeypatch, proof="pending")
    assert _run(out, mirrors, seeds)["guards"] == []
    assert _gate(json.loads((out / gs.RESULT_NAME).read_text())).status == "insufficient"
    # an ad hoc run over unproven guards still doesn't count toward the gate
    r = _run(out, mirrors, seeds, statuses=("proven", "pending"))
    assert r["summary"]["caught"] == 2
    gate = _gate(r)
    assert gate.status == "insufficient" and gate.note == "no proven guards"


def test_stale_or_missing_results_are_insufficient() -> None:
    fresh = {"at": datetime.now(UTC).isoformat(), "guards": [
        {"pack": "p", "repo": "r", "proof_status": "proven",
         "seeds": [{"seed": "a", "status": "caught"}, {"seed": "b", "status": "caught"}]}]}
    assert _gate(fresh).status == "pass"
    stale = {**fresh, "at": (datetime.now(UTC) - timedelta(days=20)).isoformat()}
    assert _gate(stale).status == "insufficient" and "days old" in _gate(stale).note
    assert _gate(None).status == "insufficient"


def test_min_sample_scales_with_proven_guards() -> None:
    one_each = {"at": datetime.now(UTC).isoformat(), "guards": [
        {"pack": f"p{i}", "repo": "r", "proof_status": "proven", "seeds": [{"seed": "a", "status": "caught"}]}
        for i in range(3)]}
    gate = _gate(one_each)  # one seed per guard can't pass it
    assert gate.min_n == 6 and gate.status == "insufficient"


def test_a_static_rule_catches_a_seed_its_test_cannot_see(tmp_path: Path, monkeypatch) -> None:
    """The proof test only checks its fixture; the rule covers the repo, so seeds go anywhere."""
    out, mirrors, seeds = _setup(tmp_path, monkeypatch, rule={"tool": "ruff", "select": ["S110", "S112"]})
    repo = tmp_path / "mirror" / "demo"
    (repo / "tests/test_guard.py").write_text(PROOF.format(kinds="(ast.Pass,)").split("def test_repo_is_clean")[0])
    _git(repo, "commit", "-qam", "fixture only")
    calls = []

    def rule_runner(source, path, rule):
        calls.append(path)
        tree = ast.parse(source)
        return [gs_hit(n.lineno, "S110" if isinstance(n.body[0], ast.Pass) else "S112", "")
                for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and len(n.body) == 1
                and isinstance(n.body[0], (ast.Pass, ast.Continue))]

    r = _run(out, mirrors, seeds, rule_runner=rule_runner)
    g = r["guards"][0]
    assert g["scope"] == "repo" and calls
    by = {s["seed"]: s for s in g["seeds"]}
    assert by["t_except_continue"]["rule"] == "caught" and by["t_except_continue"]["test"] == "missed"
    assert by["t_except_continue"]["status"] == "caught" and "ruff:S112" in by["t_except_continue"]["caught_by"]


def gs_hit(line: int, code: str, text: str):
    from auditor.class_packs import RuleHit

    return RuleHit(line, code, text)


def test_a_rule_that_cannot_run_makes_the_seed_invalid(tmp_path: Path, monkeypatch) -> None:
    out, mirrors, seeds = _setup(tmp_path, monkeypatch, rule={"tool": "ruff", "select": ["S110"]})
    r = _run(out, mirrors, seeds, rule_runner=lambda *a: None)
    assert {s["status"] for s in r["guards"][0]["seeds"]} == {"invalid"}


def test_an_unmerged_guard_is_invalid(tmp_path: Path, monkeypatch) -> None:
    out, mirrors, seeds = _setup(tmp_path, monkeypatch)
    pack = out / "agent_packs/pack-class-silent-failure.yaml"
    pack.write_text(pack.read_text().replace(_git(tmp_path / "mirror/demo", "rev-parse", "HEAD"), "f" * 40))
    g = _run(out, mirrors, seeds)["guards"][0]
    assert g["status"] == "invalid" and "isn't on the mirror" in g["note"]


def test_the_mirror_is_never_written(tmp_path: Path, monkeypatch) -> None:
    out, mirrors, seeds = _setup(tmp_path, monkeypatch)
    repo = tmp_path / "mirror" / "demo"
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    _run(out, mirrors, seeds)
    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert before == after and _git(repo, "status", "--porcelain") == ""


# ── planting and seed templates ──────────────────────────────────────────────

def test_plant_puts_imports_after_the_last_import_and_code_at_the_end() -> None:
    seed = gs.Seed(id="x", cls="c", code="def _seeded_x():\n    return json.dumps(1)\n", imports=("import json",))
    src = '"""Doc."""\nfrom __future__ import annotations\n\nimport os\n\nVALUE = os.sep\n'
    new, first, last = gs.plant(src, seed)
    lines = new.splitlines()
    assert lines[3:5] == ["import os", "import json"]
    assert lines[first - 1] == "def _seeded_x():" and last == len(lines)
    ast.parse(new)
    new, first, _ = gs.plant('"""Only a docstring."""\n', seed)
    assert new.splitlines()[1] == "import json" and new.splitlines()[first - 1].startswith("def _seeded_x")


def test_the_checked_in_seeds_are_valid() -> None:
    seeds = gs.load_seeds()
    assert {"silent_failure", "ssrf", "info_leak"} <= set(seeds)
    assert all(len(v) >= 2 for v in seeds.values())  # min_n is two seeds per guard


def test_a_seed_without_its_marker_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "s.yaml"
    bad.write_text("c:\n  - id: a\n    code: |\n      def other():\n          pass\n")
    with pytest.raises(ValueError, match="_seeded_a"):
        gs.load_seeds(bad)
