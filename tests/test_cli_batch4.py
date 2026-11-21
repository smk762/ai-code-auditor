"""``ai-audit eval trust`` output and the CLI role gate (``cli._enforce_cli_role``)."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import cli


def _gate(**kw):
    g = {"id": "g", "value": 0.5, "threshold": 0.9, "op": ">=", "n": 10, "min_n": 5,
         "status": "fail", "issue": 1, "note": ""}
    return {**g, **kw}


def _report(gates):
    return {"gates": gates,
            "summary": {"pass": 0, "fail": len(gates), "insufficient": 0, "pending": 0},
            "exit": {"gates_two_weeks": {"met": False},
                     "metrics_four_weeks": {"met": False, "note": "n/a"}, "met": False}}


def _eval_args(**kw):
    return SimpleNamespace(**{"seeded": False, "repos": "", "json": False, "no_record": True, **kw})


def _patch_eval(monkeypatch, tmp_path, rep):
    from auditor import eval_seeded, trust_eval

    monkeypatch.setattr(cli, "_output_dir_default", lambda: str(tmp_path))
    monkeypatch.setattr(trust_eval, "evaluate", lambda out, record=True: rep)
    monkeypatch.setattr(eval_seeded, "run", lambda out, repos=None: {
        "fix": {"detected": 3, "n": 4}, "control": {"wrongly_accepted": 0, "n": 4},
        "repos_with_scenarios": ["r1"], "seconds": 1})


def test_seeded_json_output_stays_one_json_document(monkeypatch, tmp_path, capsys) -> None:
    _patch_eval(monkeypatch, tmp_path, _report([_gate()]))
    cli._run_eval_trust(_eval_args(seeded=True, json=True))
    out = capsys.readouterr()
    payload = json.loads(out.out)  # raised before: the summary line came first
    assert payload["seeded"]["fix"]["detected"] == 3
    assert "seeded eval: 3/4" in out.err


def test_seeded_summary_is_on_stdout_without_json(monkeypatch, tmp_path, capsys) -> None:
    _patch_eval(monkeypatch, tmp_path, _report([_gate()]))
    cli._run_eval_trust(_eval_args(seeded=True))
    assert "seeded eval: 3/4" in capsys.readouterr().out


def test_count_threshold_is_not_shown_as_a_percentage(monkeypatch, tmp_path, capsys) -> None:
    _patch_eval(monkeypatch, tmp_path, _report([_gate(id="count", value=3, threshold=5),
                                                _gate(id="ratio", value=0.5, threshold=0.9)]))
    cli._run_eval_trust(_eval_args())
    lines = {ln.split()[1]: ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("FAIL")}
    assert ">= 5 " in lines["count"] and "500%" not in lines["count"]
    assert ">= 90%" in lines["ratio"]


# --- role gate --------------------------------------------------------------------------------

def _main(monkeypatch, argv, *, auth_mode):
    ran = []
    monkeypatch.setattr(sys, "argv", ["ai-audit", *argv])
    monkeypatch.delenv("AI_AUDIT_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_ecosystem_config",
                        lambda: SimpleNamespace(auth_mode=auth_mode, auth_code_env="X", repos=[]))
    monkeypatch.setattr(cli, "require_auth", lambda *a, **k: None)
    monkeypatch.setattr(cli, "run_repo_audit", lambda: ran.append("repo-run"))
    monkeypatch.setattr(cli, "run_benchmark", lambda *a, **k: ran.append("qa"))
    return ran


def test_repo_run_without_jwt_is_refused_when_auth_is_on(monkeypatch) -> None:
    ran = _main(monkeypatch, ["repo-run"], auth_mode=True)
    with pytest.raises(PermissionError, match="admin JWT"):
        cli.main()
    assert ran == []


def test_repo_run_without_jwt_still_runs_with_auth_off(monkeypatch) -> None:
    # The host default: nightly timers run `ai-audit repo-run` with no JWT.
    ran = _main(monkeypatch, ["repo-run"], auth_mode=False)
    cli.main()
    assert ran == ["repo-run"]


def test_qa_benchmark_without_jwt_is_refused_when_auth_is_on(monkeypatch) -> None:
    ran = _main(monkeypatch, ["qa-benchmark"], auth_mode=True)
    with pytest.raises(PermissionError, match="analyst JWT"):
        cli.main()
    assert ran == []


def test_a_given_jwt_is_always_role_checked(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(cli, "enforce_role", lambda tok, role, action: seen.append((tok, role, action)))
    cli._enforce_cli_role(SimpleNamespace(auth_mode=False), "tok", "admin", "cli:x")
    assert seen == [("tok", "admin", "cli:x")]


def test_read_only_repair_without_jwt_is_refused_when_auth_is_on(monkeypatch) -> None:
    import requests

    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(k))
    monkeypatch.delenv("AI_AUDIT_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_ecosystem_config",
                        lambda: SimpleNamespace(auth_mode=True, auth_code_env="X"))
    monkeypatch.setattr(cli, "require_auth", lambda *a, **k: None)
    with pytest.raises(PermissionError, match="analyst JWT"):
        cli._run_repair(repo_name="r", repair_api_url="http://rag.test", pack_id="ext-1")
    assert sent == []
