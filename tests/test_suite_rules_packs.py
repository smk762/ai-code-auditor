"""Regression tests for auditor/suite_rules.py robustness (export cleanup, malformed scan output)."""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from auditor import suite_rules


def _catalogue(tmp_path: Path, stdout: object) -> Path:
    d = tmp_path / "suite-rules"
    d.mkdir()
    (d / "catalogue.toml").write_text("version = 1\n")
    (d / "check.py").write_text(textwrap.dedent(f"""
        print({json.dumps(json.dumps(stdout))})
    """))
    return d


@pytest.mark.parametrize("stdout", [
    [], "oops", None, 3,
    {"rules": [], "hits": [{"rule": "r", "path": "a.py", "line": 1}]},
    {"rules": {"r": "not-a-dict"}, "hits": [{"rule": "r", "path": "a.py", "line": 1}]},
    {"rules": {"r": {"kind": "guard"}}, "hits": ["x", None, 5]},
    {"rules": {"r": {"kind": "guard"}}, "hits": "nope"},
])
def test_malformed_scan_output_never_raises(tmp_path: Path, stdout) -> None:
    assert suite_rules.scan("repo", tmp_path, cdir=_catalogue(tmp_path, stdout)) == []


def test_good_hits_survive_next_to_malformed_ones(tmp_path: Path) -> None:
    out = {"rules": {"r": {"kind": "guard", "class": "injection", "message": "Bad. More."}},
           "hits": ["x", {"rule": "r", "path": "a.py", "line": "seven"}, {"rule": "r", "path": "b.py", "line": 4}]}
    got = suite_rules.scan("repo", tmp_path, cdir=_catalogue(tmp_path, out))
    assert [(Path(f.file_path).name, f.line) for f in got] == [("a.py", 0), ("b.py", 4)]


def test_failed_extraction_leaves_no_tmp_dir(tmp_path: Path, monkeypatch) -> None:
    m = tmp_path / "argus-ci"
    m.mkdir()
    real_run = subprocess.run

    def run(*a: str) -> None:
        real_run(["git", *a], cwd=m, check=True, capture_output=True)

    run("init", "-q", "-b", "dev")
    (m / "suite-rules").mkdir()
    (m / "suite-rules" / "check.py").write_text("print('v1')\n")
    run("add", ".")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "rules")

    def corrupt_archive(cmd, *a, **kw):
        out = real_run(cmd, *a, **kw)
        if "archive" in cmd:
            out = subprocess.CompletedProcess(cmd, 0, stdout=b"not a tarball" * 100, stderr=b"")
        return out

    monkeypatch.setattr(suite_rules.subprocess, "run", corrupt_archive)
    cache = tmp_path / "cache"
    assert suite_rules.export_catalogue(m, cache) is None
    assert not [p for p in cache.iterdir() if p.name.startswith(".tmp-")]
