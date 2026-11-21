"""conductor.scanners: 64-bit fingerprints, no stale outputs, malformed SARIF skipped."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import conductor.scanners as sc
from auditor.contracts import Finding


def test_fingerprint_is_64_bits() -> None:
    fp = sc.fingerprint("semgrep", "a.py", "foo")
    assert len(fp) == 16
    assert fp == hashlib.sha256(b"semgrep|a.py|foo").hexdigest()[:16]


def test_legacy_8_char_disposition_still_suppresses() -> None:
    f = Finding(id="x", type="semgrep", severity="LOW", repo="r", file_path="a.py", line=1,
                title="t", description="t", evidence="", recommendation="", source="semgrep")
    legacy = hashlib.sha256(b"semgrep|a.py|t").hexdigest()[:8]
    assert sc.filter_by_dispositions([f], {legacy: {"decision": "rejected"}}) == []
    assert sc.filter_by_dispositions([f], {}) == [f]


def test_stale_tool_versions_from_an_earlier_run_are_not_read(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "tool-versions.txt").write_text("semgrep 0.0.1 (from another repo's run)\n", encoding="utf-8")
    monkeypatch.setattr(sc, "_run_in_group", lambda *a, **kw: 2)
    res = sc.run_scanners(tmp_path, out)
    assert res.tool_versions == ""
    assert not (out / "tool-versions.txt").exists()


@pytest.mark.parametrize("content", [
    b"[]",
    b"42",
    b'{"runs": "nope"}',
    b'{"runs": [1, "x", null]}',
    b'{"runs": [{"results": [{"ruleId": "r", "message": "plain", "locations": ["x"]}]}]}',
    b"\xff\xfe\x00garbage",
])
def test_malformed_sarif_is_skipped_not_fatal(tmp_path: Path, content: bytes) -> None:
    p = tmp_path / "x.sarif"
    p.write_bytes(content)
    out = sc.sarif_to_findings(p, source="semgrep", repo="r")
    assert isinstance(out, list)


def test_non_numeric_start_line_falls_back_to_zero(tmp_path: Path) -> None:
    p = tmp_path / "x.sarif"
    p.write_text(json.dumps({"runs": [{"results": [{
        "ruleId": "r", "message": {"text": "m"},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "a.py"},
                                            "region": {"startLine": "ten"}}}],
    }]}]}), encoding="utf-8")
    (f,) = sc.sarif_to_findings(p, source="semgrep", repo="r")
    assert f.line == 0 and f.file_path == "a.py"
