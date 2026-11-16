"""Reliability gates (#63) and the seeded-defect eval of the evidence bar."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from auditor import eval_seeded, trust_eval, validator
from auditor.trust_eval import build_gates, gates_exit, metrics_exit

NOW = datetime.now(UTC)


def by_id(gates) -> dict:
    return {g.id: g for g in gates}


def trust(**kw) -> dict:
    base = {
        "false_resolve": {"by_evidence": {"repro": 18, "tests": 4, "waiver": 2}, "lint_only_since_bar": 1},
        "recurrence": {"rate": 0.02, "observed": 120},
        "agent_prs": {"mature_rate": 0.0, "mature": 25},
        "scan_persistence": {"rate": 0.91, "findings": 3000},
        "gc": {"rate": 0.05, "closed": 40},
    }
    base.update(kw)
    return base


def seeded(fix=(True,) * 5, control=(False,) * 5, age_days=1) -> dict:
    at = (NOW - timedelta(days=age_days)).isoformat()
    return {"at": at, "scenarios": [{"case": "fix", "detected": d} for d in fix]
            + [{"case": "control", "detected": d} for d in control]}


# ── gates ────────────────────────────────────────────────────────────────────

def test_all_built_gates_pass_on_good_numbers() -> None:
    g = by_id(build_gates(trust(), seeded(), None, {"n": 12, "held": 12, "rate": 1.0}))
    live = {k: v.status for k, v in g.items() if v.status != "pending" and not k.startswith("classes.")}
    assert set(live.values()) == {"pass"}, live
    assert g["evidence.lint_only"].value == pytest.approx(1 / 24)
    assert {k for k, v in g.items() if v.status == "pending"} == {"rules.fixtures", "arch.contracts"}
    assert g["classes.precision"].status == "insufficient"  # no gold set in tests


def test_failing_and_insufficient() -> None:
    g = by_id(build_gates(trust(recurrence={"rate": 0.2, "observed": 120},
                                scan_persistence={"rate": 0.95, "findings": 10}), seeded(fix=(True, False) * 3)))
    assert g["trust.regressed"].status == "fail"
    assert g["scan.persistence"].status == "insufficient"  # good value, too few findings
    assert g["evidence.seeded_recall"].status == "fail" and g["evidence.seeded_recall"].value == 0.5


def test_one_false_proof_fails_the_control_gate() -> None:
    g = by_id(build_gates(trust(), seeded(control=(False, False, False, False, True))))
    assert g["evidence.seeded_false_proof"].status == "fail"


def test_stale_or_missing_seeded_eval_is_insufficient() -> None:
    g = by_id(build_gates(trust(), seeded(age_days=20)))
    assert g["evidence.seeded_recall"].status == "insufficient" and "days old" in g["evidence.seeded_recall"].note
    g = by_id(build_gates(None, None))
    assert {v.status for v in g.values()} <= {"insufficient", "pending"}  # nothing passes on no data


def test_gates_exit_needs_two_green_weeks() -> None:
    green = {"a": "pass", "b": "pass", "c": "pending"}
    red = {"a": "pass", "b": "fail"}
    wk = lambda d: (NOW - timedelta(days=d)).isoformat()
    assert gates_exit([{"at": wk(8), "statuses": green}, {"at": wk(0), "statuses": green}])["met"]
    assert not gates_exit([{"at": wk(0), "statuses": green}])["met"]
    # the week's *last* evaluation counts: green early in the week, red at the end
    assert not gates_exit([{"at": wk(8), "statuses": green}, {"at": wk(0), "statuses": red}])["met"]
    insufficient = {"a": "pass", "b": "insufficient"}
    assert not gates_exit([{"at": wk(8), "statuses": insufficient}, {"at": wk(0), "statuses": green}])["met"]
    assert not gates_exit([{"at": wk(8), "statuses": {"x": "pending"}}, {"at": wk(0), "statuses": {"x": "pending"}}])["met"]


def test_metrics_exit_over_four_weeks() -> None:
    def rows(fr: list[float], persist: list[float]) -> list[dict]:
        return [{"at": (NOW - timedelta(weeks=3 - i)).isoformat(), "false_resolve_rate": f, "scan_persistence": p}
                for i, (f, p) in enumerate(zip(fr, persist, strict=True))]
    assert metrics_exit(rows([0.8, 0.5, 0.2, 0.05], [0.9, 0.9, 0.91, 0.9]))["met"]
    worse = metrics_exit(rows([0.05, 0.05, 0.1, 0.2], [0.9, 0.9, 0.9, 0.9]))
    assert not worse["met"] and worse["worse"][0]["metric"] == "false_resolve_rate"
    assert not metrics_exit(rows([0.9, 0.9, 0.9, 0.85], [0.9, 0.9, 0.9, 0.80]))["met"]  # persistence dropped
    assert metrics_exit(rows([0.8, 0.5, 0.2, 0.05], [0.9] * 4)[:3])["note"] == "3 of 4 weekly snapshots"


def test_evaluate_writes_report_and_history(tmp_path: Path) -> None:
    (tmp_path / "trust.json").write_text(json.dumps(trust()))
    (tmp_path / trust_eval.SEEDED_NAME).write_text(json.dumps(seeded()))
    r1 = trust_eval.evaluate(tmp_path)
    trust_eval.evaluate(tmp_path)
    assert r1["summary"]["fail"] == 0 and r1["summary"]["pending"] == 2
    assert r1["exit"]["met"] is False  # pending components and no four-week history
    assert len((tmp_path / trust_eval.HISTORY_NAME).read_text().splitlines()) == 2
    assert json.loads((tmp_path / trust_eval.REPORT_NAME).read_text())["at"]
    trust_eval.evaluate(tmp_path, record=False)
    assert len((tmp_path / trust_eval.HISTORY_NAME).read_text().splitlines()) == 2


def test_evaluate_quietly_refreshes_a_week_old_seeded_eval(tmp_path: Path, monkeypatch) -> None:
    calls = []
    (tmp_path / trust_eval.SEEDED_NAME).write_text(json.dumps(seeded(age_days=1)))
    trust_eval.evaluate_quietly(tmp_path, seeded_runner=lambda out: calls.append(out))
    assert calls == []
    (tmp_path / trust_eval.SEEDED_NAME).write_text(json.dumps(seeded(age_days=9)))
    trust_eval.evaluate_quietly(tmp_path, seeded_runner=lambda out: calls.append(out))
    assert len(calls) == 1
    monkeypatch.setenv("AI_AUDIT_TRUST_EVAL", "0")
    assert "skipped" in trust_eval.evaluate_quietly(tmp_path)


def test_api_serves_the_eval(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from auditor.audit_api import app

    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(tmp_path))
    client = TestClient(app)
    assert client.get("/audit/trust/eval.json").status_code == 404
    trust_eval.evaluate(tmp_path)
    assert client.get("/audit/trust/eval.json").json()["gates"]


# ── seeded defects ───────────────────────────────────────────────────────────

SRC = '''def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def is_even(n):
    return n % 2 == 0


def both(a, b):
    return a and b
'''


def test_mutants_flip_one_operator_each() -> None:
    ms = dict(eval_seeded.mutants(SRC, "clamp"))
    assert set(ms) == {"clamp L2: Lt → GtE", "clamp L4: Gt → LtE"}
    assert "if x >= lo:" in ms["clamp L2: Lt → GtE"] and "if x > hi:" in ms["clamp L2: Lt → GtE"]
    assert "return a or b" in dict(eval_seeded.mutants(SRC, "both"))["both L14: And → Or"]
    assert eval_seeded.mutants(SRC, "missing") == [] and eval_seeded.mutants("def (", "x") == []
    for _desc, src in eval_seeded.mutants(SRC, "is_even"):
        compile(src, "m", "exec")  # every mutant still parses


def test_targets_resolve_from_imports(tmp_path: Path) -> None:
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "src/pkg/calc.py").write_text(SRC)
    (tmp_path / "tests").mkdir()
    t = tmp_path / "tests/test_calc.py"
    t.write_text("from pkg.calc import clamp, is_even, NOT_A_FUNC\nimport os\n")
    assert sorted(fn for _m, fn in eval_seeded.targets(tmp_path, t)) == ["clamp", "is_even"]


def _git(cwd: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True)


def test_seeded_run_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """A real mirror with a real test: the bar proves the fix and rejects the cosmetic control."""
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache: (Path(sys.executable), ""))
    repo = tmp_path / "mirror" / "calc"
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "src/pkg/__init__.py").write_text("")
    (repo / "src/pkg/calc.py").write_text(SRC)
    (repo / "requirements.txt").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests/test_calc.py").write_text(
        "from pkg.calc import clamp\n\ndef test_low():\n    assert clamp(-5, 0, 10) == 0\n\n"
        "def test_high():\n    assert clamp(50, 0, 10) == 10\n\ndef test_mid():\n    assert clamp(5, 0, 10) == 5\n")
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    mirrors = tmp_path / "mirrors.yaml"
    mirrors.write_text(f"mirrors:\n  - name: calc\n    path: {repo}\n")
    out = tmp_path / "reports"
    out.mkdir()
    r = eval_seeded.run(out, mirrors_path=mirrors, seed="fixed")
    assert r["fix"] == {"n": 1, "detected": 1}, r["scenarios"]
    assert r["control"] == {"n": 1, "wrongly_accepted": 0}, r["scenarios"]
    fix = next(s for s in r["scenarios"] if s["case"] == "fix")
    assert fix["status"] == "resolved" and fix["kind"] == "repro" and set(fix["repro"]) & set(fix["killed"])
    assert (out / eval_seeded.SEEDED_NAME).exists()
    g = by_id(build_gates(trust(), json.loads((out / eval_seeded.SEEDED_NAME).read_text())))
    assert g["evidence.seeded_recall"].value == 1.0 and g["evidence.seeded_recall"].status == "insufficient"  # n=1 < 5
