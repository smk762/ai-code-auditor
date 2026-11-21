"""suite_rules: malformed hits are skipped, never raised, and an export never prunes a catalogue
another run may still be using."""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
import time
from collections.abc import Callable
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


@pytest.mark.parametrize("bad", [
    {"rule": ["r"], "path": "a.py"},
    {"rule": {"r": 1}, "path": "a.py"},
    {"rule": "r", "path": 7},
    {"rule": "r", "path": ["a.py"]},
    {"rule": "r", "path": {"p": 1}},
])
def test_wrongly_typed_hits_are_skipped_not_raised(tmp_path: Path, bad: dict) -> None:
    out = {"rules": {"r": {"kind": "guard", "class": "injection", "message": "Bad."}},
           "hits": [bad, {"rule": "r", "path": "b.py", "line": 2}]}
    got = suite_rules.scan("repo", tmp_path, cdir=_catalogue(tmp_path, out))
    assert [(Path(f.file_path).name, f.line) for f in got] == [("b.py", 2)]


def _mirror(tmp_path: Path) -> tuple[Path, Callable[..., str]]:
    m = tmp_path / "argus-ci"
    m.mkdir()

    def run(*a: str) -> str:
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=m, check=True,
                              capture_output=True, text=True).stdout.strip()

    run("init", "-q", "-b", "dev")
    (m / "suite-rules").mkdir()
    (m / "suite-rules" / "check.py").write_text("print('v1')\n")
    run("add", ".")
    run("commit", "-qm", "v1")
    return m, run


def test_export_keeps_a_catalogue_another_run_is_using(tmp_path: Path) -> None:
    m, run = _mirror(tmp_path)
    cache = tmp_path / "cache"
    a = suite_rules.export_catalogue(m, cache)  # run A, still using tree X
    assert a is not None
    (m / "suite-rules" / "check.py").write_text("print('v2')\n")
    run("commit", "-qam", "v2")  # a fetch moves dev before run B exports
    b = suite_rules.export_catalogue(m, cache)
    assert b is not None and b != a
    assert (a / "check.py").is_file(), "run B pruned the catalogue run A is executing"


def test_export_prunes_catalogues_unused_past_the_grace(tmp_path: Path) -> None:
    m, run = _mirror(tmp_path)
    cache = tmp_path / "cache"
    a = suite_rules.export_catalogue(m, cache)
    assert a is not None
    old = time.time() - suite_rules.PRUNE_GRACE_S - 60
    os.utime(a.parent, (old, old))
    (m / "suite-rules" / "check.py").write_text("print('v2')\n")
    run("commit", "-qam", "v2")
    b = suite_rules.export_catalogue(m, cache)
    assert b is not None and (b / "check.py").is_file()
    assert not a.parent.exists()


def test_a_cached_hit_counts_as_use(tmp_path: Path) -> None:
    m, _run = _mirror(tmp_path)
    cache = tmp_path / "cache"
    a = suite_rules.export_catalogue(m, cache)
    old = time.time() - suite_rules.PRUNE_GRACE_S - 60
    os.utime(a.parent, (old, old))
    assert suite_rules.export_catalogue(m, cache) == a
    assert a.parent.stat().st_mtime > old + 60
