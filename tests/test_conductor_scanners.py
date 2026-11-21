from __future__ import annotations

import json
from pathlib import Path

from conductor.scanners import fingerprint, sarif_to_findings


def test_fingerprint_stable():
    assert fingerprint("semgrep", "a.py", "foo") == fingerprint("semgrep", "a.py", "foo")
    assert fingerprint("semgrep", "a.py", "foo") != fingerprint("semgrep", "b.py", "foo")


def test_sarif_to_findings(tmp_path: Path):
    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "semgrep", "rules": [{"id": "x.y"}]}},
                "results": [
                    {
                        "ruleId": "x.y",
                        "level": "error",
                        "message": {"text": "bad thing"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "src/app.py"},
                                    "region": {"startLine": 10},
                                }
                            }
                        ],
                    }
                ],
            }
        ],
    }
    path = tmp_path / "semgrep.sarif"
    path.write_text(json.dumps(sarif), encoding="utf-8")
    findings = sarif_to_findings(path, source="semgrep", repo="argus-lens")
    assert len(findings) == 1
    assert findings[0].severity == "HIGH"
    assert findings[0].file_path == "src/app.py"
    assert findings[0].repo == "argus-lens"


def _result(rule: str, file: str, line: int, text: str) -> dict:
    return {
        "ruleId": rule,
        "message": {"text": text},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": file}, "region": {"startLine": line}}}],
    }


def _ids(tmp_path: Path, runs: list[list[dict]]) -> list[str]:
    path = tmp_path / "semgrep.sarif"
    path.write_text(json.dumps({"runs": [{"results": r} for r in runs]}), encoding="utf-8")
    return [f.id for f in sarif_to_findings(path, source="semgrep", repo="r")]


def test_finding_ids_do_not_shift_when_an_earlier_result_disappears(tmp_path: Path):
    a = _result("x.y", "a.py", 10, "first")
    b = _result("x.y", "a.py", 20, "second")
    both = _ids(tmp_path, [[a, b]])
    only_b = _ids(tmp_path, [[b]])
    assert both[1] == only_b[0]


def test_finding_ids_do_not_collide_across_runs(tmp_path: Path):
    ids = _ids(tmp_path, [[_result("x.y", "a.py", 10, "m")], [_result("x.y", "a.py", 99, "other")]])
    assert len(set(ids)) == 2
    dup = _result("x.y", "a.py", 10, "m")
    assert len(set(_ids(tmp_path, [[dup, dup]]))) == 2  # exact duplicates still get distinct ids


def test_failed_scanner_run_is_reported_not_clean(tmp_path: Path, monkeypatch):
    import conductor.scanners as sc

    def crash(*a, **kw):
        return 2

    monkeypatch.setattr(sc, "_run_in_group", crash)
    res = sc.run_scanners(tmp_path, tmp_path / "out")
    assert res.findings == []
    assert res.error
    assert res.exit_codes.get("run-scanners") == 2


def test_scanner_run_has_a_timeout(tmp_path: Path, monkeypatch):
    import subprocess

    import conductor.scanners as sc

    seen = {}

    def hang(*a, **kw):
        seen["timeout"] = kw.get("timeout")
        if kw.get("timeout") is None:
            raise AssertionError("scanner run without a timeout")
        raise subprocess.TimeoutExpired(a[0], kw["timeout"])

    monkeypatch.setattr(sc, "_run_in_group", hang)
    monkeypatch.setenv("AI_AUDIT_SCANNER_TIMEOUT_S", "5")
    res = sc.run_scanners(tmp_path, tmp_path / "out")
    assert seen["timeout"] == 5
    assert "timed out" in res.error
    assert res.exit_codes.get("run-scanners") == 124
