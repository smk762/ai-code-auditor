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

def guard_seeded(per_guard=((True, True),), age_days=1, proof="proven") -> dict:
    """A guard_seeded.json with one guard per entry; each bool is one valid seed (caught or missed)."""
    at = (NOW - timedelta(days=age_days)).isoformat()
    return {"at": at, "guards": [
        {"pack": f"pack-class-g{i}", "repo": "r", "proof_status": proof, "status": "ok",
         "seeds": [{"seed": f"s{j}", "status": "caught" if c else "missed"} for j, c in enumerate(seeds)]}
        for i, seeds in enumerate(per_guard)]}


def test_all_built_gates_pass_on_good_numbers() -> None:
    g = by_id(build_gates(trust(), seeded(), None, {"n": 12, "held": 12, "rate": 1.0}, {"n": 6, "ok": 6, "rate": 1.0},
                          {"n": 2, "fired": 2, "rate": 1.0, "rules": []}, guard_seeded()))
    live = {k: v.status for k, v in g.items() if v.status != "pending" and not k.startswith("classes.")}
    assert set(live.values()) == {"pass"}, live
    assert g["evidence.lint_only"].value == pytest.approx(1 / 24)
    assert not [k for k, v in g.items() if v.status == "pending"]  # every gate is built
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
    wk = lambda d: (NOW - timedelta(days=d)).isoformat()  # 7 days back is always the previous ISO week
    assert gates_exit([{"at": wk(7), "statuses": green}, {"at": wk(0), "statuses": green}])["met"]
    assert not gates_exit([{"at": wk(0), "statuses": green}])["met"]
    assert not gates_exit([{"at": wk(7), "statuses": green}, {"at": wk(0), "statuses": red}])["met"]
    insufficient = {"a": "pass", "b": "insufficient"}
    assert not gates_exit([{"at": wk(7), "statuses": insufficient}, {"at": wk(0), "statuses": green}])["met"]
    assert not gates_exit([{"at": wk(7), "statuses": {"x": "pending"}}, {"at": wk(0), "statuses": {"x": "pending"}}])["met"]


def test_the_last_evaluation_of_a_week_wins() -> None:
    green = {"a": "pass", "b": "pass"}
    red = {"a": "pass", "b": "fail"}
    monday = datetime.combine(NOW.date() - timedelta(days=NOW.isocalendar()[2] - 1), datetime.min.time(), UTC)
    prior = {"at": (monday - timedelta(days=6)).isoformat(), "statuses": green}
    early, late = (monday + timedelta(hours=1)).isoformat(), (monday + timedelta(hours=2)).isoformat()
    # both evaluations fall in the current ISO week; only the later one counts
    assert not gates_exit([prior, {"at": early, "statuses": green}, {"at": late, "statuses": red}])["met"]
    assert gates_exit([prior, {"at": early, "statuses": red}, {"at": late, "statuses": green}])["met"]


def test_gates_exit_needs_consecutive_recent_weeks() -> None:
    green = {"a": "pass"}
    wk = lambda d: (NOW - timedelta(days=d)).isoformat()
    # green, then ten weeks with no evaluation, then green: the missing weeks are not green
    assert not gates_exit([{"at": wk(77), "statuses": green}, {"at": wk(0), "statuses": green}])["met"]
    # two adjacent green weeks long ago and nothing since: stale evidence
    assert not gates_exit([{"at": wk(70), "statuses": green}, {"at": wk(63), "statuses": green}])["met"]
    # last week and the week before still count before this week's first run
    assert gates_exit([{"at": wk(14), "statuses": green}, {"at": wk(7), "statuses": green}])["met"]


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
    assert r1["summary"]["fail"] == 0 and r1["summary"]["pending"] == 0
    assert {g["id"]: g["status"] for g in r1["gates"]}["arch.contracts"] == "pass"  # the checked-in rules
    assert r1["exit"]["met"] is False  # insufficient gates and no four-week history
    assert len((tmp_path / trust_eval.HISTORY_NAME).read_text().splitlines()) == 2
    assert json.loads((tmp_path / trust_eval.REPORT_NAME).read_text())["at"]
    trust_eval.evaluate(tmp_path, record=False)
    assert len((tmp_path / trust_eval.HISTORY_NAME).read_text().splitlines()) == 2


def test_evaluate_quietly_refreshes_a_week_old_seeded_eval(tmp_path: Path, monkeypatch) -> None:
    calls = []
    (tmp_path / trust_eval.SEEDED_NAME).write_text(json.dumps(seeded(age_days=1)))
    (tmp_path / trust_eval.GUARD_SEEDED_NAME).write_text(json.dumps(guard_seeded(age_days=1)))
    guard_calls = []
    quietly = lambda: trust_eval.evaluate_quietly(tmp_path, seeded_runner=lambda out: calls.append(out),
                                                  guard_runner=lambda out: guard_calls.append(out))
    quietly()
    assert calls == [] and guard_calls == []
    (tmp_path / trust_eval.SEEDED_NAME).write_text(json.dumps(seeded(age_days=9)))
    quietly()
    assert len(calls) == 1 and guard_calls == []
    (tmp_path / trust_eval.GUARD_SEEDED_NAME).write_text(json.dumps(guard_seeded(age_days=9)))
    quietly()
    assert len(guard_calls) == 1
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
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache, extra=None: (Path(sys.executable), ""))
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


def test_evidence_gates_judge_the_rolling_window_when_the_report_has_one() -> None:
    fr = {"by_evidence": {"repro": 10, "waiver": 90}, "lint_only_since_bar": 80,
          "window": {"days": 14, "resolutions": 40, "lint_only": 1, "waived": 4}}
    g = by_id(build_gates(trust(false_resolve=fr), seeded()))
    assert g["evidence.lint_only"].value == pytest.approx(1 / 40) and g["evidence.lint_only"].n == 40
    assert g["evidence.waiver_rate"].value == pytest.approx(4 / 40)
    assert "last 14 days" in g["evidence.lint_only"].description


def test_read_jsonl_skips_undecodable_bytes_and_non_object_lines(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    p.write_bytes(b'{"a": 1}\n\xff\xfe garbage\n1\n"x"\nnull\n[1]\n{"b": 2}\n')
    assert trust_eval._read_jsonl(p) == [{"a": 1}, {"b": 2}]


def test_seeded_run_creates_a_missing_output_dir(tmp_path: Path) -> None:
    mirrors = tmp_path / "mirrors.yaml"
    mirrors.write_text("mirrors: []\n")
    out = tmp_path / "fresh" / "reports"
    r = eval_seeded.run(out, mirrors_path=mirrors, seed="fixed")
    assert json.loads((out / eval_seeded.SEEDED_NAME).read_text())["seed"] == r["seed"] == "fixed"


def test_kill_restores_the_module_byte_for_byte(tmp_path: Path, monkeypatch) -> None:
    import random

    from auditor import evidence

    mod = tmp_path / "calc.py"
    raw = SRC.replace("\n", "\r\n").encode()
    mod.write_bytes(raw)
    seen: list[bytes] = []

    def fake_pytest(root, python, files, timeout_s):
        seen.append(mod.read_bytes())
        return {"t::a": "failed" if len(seen) > 1 else "passed"}, ""

    monkeypatch.setattr(evidence, "_pytest", fake_pytest)
    hit = eval_seeded._kill(tmp_path, tmp_path, mod, "clamp", "t.py", Path(sys.executable), 5, tmp_path,
                            random.Random(0))
    assert hit is not None and hit[2] == ["t::a"] and seen[1] != raw
    assert mod.read_bytes() == raw


def test_arch_contracts_gate_is_measured_and_cannot_pass_on_no_rules() -> None:
    one_dead = {"n": 2, "fired": 1, "rate": 0.5, "rules": [{"rule": "a", "fired": True},
                                                           {"rule": "use_x", "fired": False}]}
    g = by_id(build_gates(trust(), seeded(), arch_contracts=one_dead))["arch.contracts"]
    assert g.status == "fail" and "use_x" in g.note
    assert by_id(build_gates(trust(), seeded(), arch_contracts={"n": 0, "fired": 0, "rate": None, "rules": []})
                 )["arch.contracts"].status == "insufficient"
    assert by_id(build_gates(trust(), seeded()))["arch.contracts"].status == "insufficient"


def test_arch_contracts_eval_reads_the_real_rules_and_reports_a_broken_file(tmp_path: Path) -> None:
    rep = trust_eval.arch_contracts_eval()  # config/architecture_rules.yaml
    assert rep["n"] >= 2 and rep["rate"] == 1.0, rep
    bad = tmp_path / "rules.yaml"
    bad.write_text("rules:\n  - name: r\n    forbid:\n      - from_roles: [gateway]\n        to_roles: [database]\n")
    rep = trust_eval.arch_contracts_eval(bad)
    assert rep["n"] == 0 and rep["rate"] is None and "gateway" in rep["error"]
    gate = by_id(build_gates(trust(), seeded(), arch_contracts=rep))["arch.contracts"]
    assert gate.status == "insufficient" and "gateway" in gate.note
