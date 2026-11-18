"""Pack GC edge cases: misses < 1, unknown file history, and a Mirror with no usable repo."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from auditor import trust_metrics as tm
from auditor.pack_gc import Mirror, plan

T0 = datetime(2026, 11, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=5)


def _pack(pid: str, *, seen: datetime | None = T0) -> tm.PackView:
    prov = {"run_id": "r0"}
    if seen is not None:
        prov["detected_at"] = seen.isoformat()
    return tm.pack_view({
        "work_item_id": pid, "kind": "nightly", "status": "open", "repo": {"name": "x"},
        "location": {"file_path": "app/calc.py", "line": 2, "code_snippet": ""},
        "narrative": {"title": "add subtracts instead of adding"}, "provenance": prov, "governance": {},
    })


def _scan(run: str, hours: float) -> tm.Scan:
    return tm.Scan(run_id=run, repo="x", at=T0 + timedelta(hours=hours), findings=[])


def test_zero_misses_never_closes_and_a_missing_date_does_not_crash() -> None:
    r = plan([_pack("a"), _pack("b", seen=None)], [], {}, now=NOW, misses=0)
    assert r["closures"] == []


def test_no_mirror_does_not_claim_the_file_is_unchanged() -> None:
    scans = [_scan(f"s{i}", 24 * i) for i in (1, 2, 3)]
    r = plan([_pack("a")], scans, {}, now=NOW, misses=3)
    [c] = r["closures"]
    assert c.rule == "not_rescanned"
    assert "unchanged" not in c.reason and "detector drift" not in c.reason
    assert "history unavailable" in c.reason


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_mirror_without_a_usable_ref_never_reads_the_cwd_or_index(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "staged-only"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("secret = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")  # in the index, no commit: no ref resolves
    monkeypatch.chdir(repo)
    for m in (Mirror(None), Mirror(repo)):
        assert not m.ok
        assert m.lines("a.py") is None
        assert m.files() == set()
        assert m.last_change("a.py") is None
