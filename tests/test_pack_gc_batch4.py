"""Pack GC: lenient git decoding, unquoted ls-tree paths, and per-status mass-closure holds."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from auditor import trust_metrics as tm
from auditor.pack_gc import Mirror, plan

T0 = datetime(2026, 11, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=5)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def _repo(tmp_path: Path, files: dict[str, bytes]) -> Path:
    repo = tmp_path / "m"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name, data in files.items():
        (repo / name).write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return repo


def test_latin1_and_binary_blobs_are_read_without_raising(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"legacy.py": "name = 'caf\xe9'\n".encode("latin-1"), "blob.bin": b"\xff\xfe\x00\x81"})
    m = Mirror(repo)
    assert m.ok
    lines = m.lines("legacy.py")
    assert lines is not None and lines[0].startswith("name = 'caf")
    assert m.lines("blob.bin") is not None


def test_non_ascii_paths_are_listed_as_real_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"café.py": b"x = 1\n", 'we"ird.py': b"y = 2\n", "plain.py": b"z = 3\n"})
    m = Mirror(repo)
    assert m.files() == {"café.py", 'we"ird.py', "plain.py"}
    assert m.lines("café.py") == ["x = 1"]


class _FakeMirror(Mirror):
    def __init__(self, files: set[str]):
        self.path, self.ref = Path("/m"), "origin/dev"
        self._files, self._text = set(files), {}

    def last_change(self, path: str) -> datetime | None:
        return None


def _pack(pid: str, status: str, path: str) -> tm.PackView:
    return tm.pack_view({
        "work_item_id": pid, "kind": "nightly", "status": status, "repo": {"name": "x"},
        "location": {"file_path": path, "line": 1, "code_snippet": ""},
        "narrative": {"title": f"t {pid}"}, "provenance": {"detected_at": T0.isoformat(), "run_id": "r0"},
        "governance": {},
    })


def test_a_hold_on_one_status_keeps_the_other_status_closing() -> None:
    m = {"x": _FakeMirror({"keep.py"})}
    opens = [_pack(f"o{i}", "open", f"gone{i}.py") for i in range(12)] + [_pack("ok", "open", "keep.py")]
    wontfix = [_pack(f"w{i}", "wontfix", f"wgone{i}.py") for i in range(2)]
    wontfix += [_pack(f"wk{i}", "wontfix", "keep.py") for i in range(5)]
    r = plan(opens + wontfix, [], m, now=NOW)
    closed = {c.pack_id for c in r["closures"]}
    assert closed == {"w0", "w1"}  # the open hold doesn't swallow the wontfix closures
    assert "12 of 13 open" in r["held"]["x"] and "wontfix" not in r["held"]["x"]


def test_both_statuses_tripping_report_both_reasons() -> None:
    m = {"x": _FakeMirror(set())}
    packs = [_pack(f"o{i}", "open", f"gone{i}.py") for i in range(12)]
    packs += [_pack(f"w{i}", "wontfix", f"wgone{i}.py") for i in range(11)]
    r = plan(packs, [], m, now=NOW)
    assert r["closures"] == []
    msg = r["held"]["x"]
    assert "12 of 12 open" in msg and "11 of 11 wontfix" in msg and msg.endswith("rerun with force")
