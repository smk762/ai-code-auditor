"""conductor.scanners: a timeout kills the whole scanner process group, a missing script is not
a clean scan, and SARIF rule references and default levels are honoured."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import conductor.scanners as sc


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"  # a zombie has already exited


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_timeout_kills_the_scanners_the_script_started(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    with (tmp_path / "log").open("w") as log, pytest.raises(subprocess.TimeoutExpired):
        sc._run_in_group(["bash", "-c", script], env=dict(os.environ), stdout=log, timeout=1)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), "scanner grandchild outlived the timed-out run"


def test_run_in_group_returns_the_exit_code(tmp_path: Path) -> None:
    with (tmp_path / "log").open("w") as log:
        assert sc._run_in_group(["bash", "-c", "exit 3"], env=dict(os.environ), stdout=log, timeout=10) == 3


def test_missing_script_is_not_a_clean_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point the script lookup at a tree without scripts/security/run-scanners.sh.
    monkeypatch.setattr(sc, "__file__", str(tmp_path / "pkg" / "conductor" / "scanners.py"))
    out = tmp_path / "out"
    out.mkdir()
    (out / "tool-versions.txt").write_text("semgrep 0.0.1 (earlier run)\n", encoding="utf-8")
    (out / "exit-codes.txt").write_text("semgrep=0\n", encoding="utf-8")
    res = sc.run_scanners(tmp_path, out)
    assert res.tool_versions == ""
    assert res.exit_codes == {}
    assert "run-scanners.sh missing" in res.error


def _sarif(tmp_path: Path, run: dict) -> Path:
    p = tmp_path / "x.sarif"
    p.write_text(json.dumps({"version": "2.1.0", "runs": [run]}), encoding="utf-8")
    return p


def _result(**kw: object) -> dict:
    return {"message": {"text": "m"}, "locations": [{"physicalLocation": {
        "artifactLocation": {"uri": "a.py"}, "region": {"startLine": 1}}}], **kw}


def test_rule_found_via_rule_id_object_or_index(tmp_path: Path) -> None:
    rules = [{"id": "first-rule"}, {"id": "second-rule"}]
    p = _sarif(tmp_path, {"tool": {"driver": {"rules": rules}}, "results": [
        _result(rule={"id": "by-ref"}),
        _result(ruleIndex=1),
        _result(rule={"index": 0}),
        _result(ruleIndex=9),
    ]})
    found = sc.sarif_to_findings(p, source="semgrep", repo="r")
    assert [json.loads(f.evidence)["rule"] for f in found] == ["by-ref", "second-rule", "first-rule", "unknown"]
    assert json.loads(found[1].evidence)["rule_meta"] == {"id": "second-rule"}


def test_missing_level_takes_the_rules_default(tmp_path: Path) -> None:
    rules = [
        {"id": "err", "defaultConfiguration": {"level": "error"}},
        {"id": "nit", "defaultConfiguration": {"level": "note"}},
        {"id": "plain"},
    ]
    p = _sarif(tmp_path, {"tool": {"driver": {"rules": rules}}, "results": [
        _result(ruleId="err"), _result(ruleId="nit"), _result(ruleId="plain"),
        _result(ruleId="nit", level="error"),
    ]})
    found = sc.sarif_to_findings(p, source="semgrep", repo="r")
    assert [f.severity for f in found] == ["HIGH", "LOW", "MEDIUM", "HIGH"]
