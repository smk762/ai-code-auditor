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
