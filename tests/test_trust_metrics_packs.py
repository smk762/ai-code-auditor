"""Regression tests for auditor/trust_metrics.py audit packs (robust loading, ordering, git parsing)."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from auditor import trust_metrics as tm
from auditor.db import AuditRun, FindingRecord

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def _scan(run: str, hours: float, findings=(), repo: str = "x") -> tm.Scan:
    return tm.Scan(run_id=run, repo=repo, at=T0 + timedelta(hours=hours),
                   findings=[{"path": p, "title": t, "line": ln, "type": "bug"} for p, t, ln in findings])


def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(cwd), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True).stdout.decode().strip()


# ── load_history ─────────────────────────────────────────────────────────────

def _history(tmp_path: Path, body: bytes) -> Path:
    (tmp_path / tm.HISTORY_NAME).write_bytes(body)
    return tmp_path


def test_history_skips_non_object_lines(tmp_path: Path) -> None:
    out = _history(tmp_path, b'{"a": 1}\n123\n"x"\nnull\n[1]\n{"b": 2}\n')
    assert tm.load_history(out) == [{"a": 1}, {"b": 2}]


@pytest.mark.parametrize("limit", [0, -1, -5])
def test_history_non_positive_limit_is_empty(tmp_path: Path, limit: int) -> None:
    out = _history(tmp_path, b'{"a": 1}\n{"b": 2}\n{"c": 3}\n')
    assert tm.load_history(out, limit=limit) == []
    assert tm.load_history(out, limit=2) == [{"b": 2}, {"c": 3}]


def test_history_with_undecodable_bytes_keeps_the_good_lines(tmp_path: Path) -> None:
    out = _history(tmp_path, b'{"a": 1}\n{"b": "\xff\xfe"\n{"c": 3}\n\xff\n')
    got = tm.load_history(out)
    assert {"a": 1} in got and {"c": 3} in got


# ── load_packs ───────────────────────────────────────────────────────────────

def test_non_utf8_pack_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    d = tmp_path / "agent_packs"
    d.mkdir()
    (d / "bad.yaml").write_bytes(b"work_item_id: bad\ntitle: caf\xe9\n")
    (d / "good.yaml").write_text("work_item_id: good\nkind: nightly\nstatus: open\n")
    assert [p.id for p in tm.load_packs(tmp_path)] == ["good"]


# ── rel_path ─────────────────────────────────────────────────────────────────

def test_rel_path_keeps_a_subdirectory_named_like_the_repo() -> None:
    assert tm.rel_path("x", "/home/smk/mirrors/x/x/foo.py") == "x/foo.py"
    assert tm.rel_path("x", "/home/smk/mirrors/x/foo.py") == "foo.py"


# ── scan ordering ────────────────────────────────────────────────────────────

def test_persistence_does_not_depend_on_input_order() -> None:
    s1 = _scan("s1", 0, [("a.py", "Race on write", 1), ("a.py", "Gone", 9)])
    s2 = _scan("s2", 10, [("a.py", "Race on write", 1)])
    s3 = _scan("s3", 20, [("a.py", "Race on write", 1)])
    fwd = tm.scan_persistence_metric([s1, s2, s3], [])
    rev = tm.scan_persistence_metric([s3, s2, s1], [])
    assert (rev["findings"], rev["refound"]) == (fwd["findings"], fwd["refound"]) == (3, 2)
    assert tm.flow_metric([s3, s2, s1]) == tm.flow_metric([s1, s2, s3])


def test_pairs_keep_the_most_recent_across_repos() -> None:
    scans = []
    for i in range(70):  # repo "a" scanned recently, repo "z" long ago
        scans.append(_scan(f"z{i}", i, [("f.py", "Bug", 1)], repo="z"))
        scans.append(_scan(f"a{i}", 1000 + i, [("f.py", "Bug", 1)], repo="a"))
    pairs = tm.scan_persistence_metric(scans, [])["pairs"]
    assert len(pairs) == 60 and {p["repo"] for p in pairs} == {"a"}
    assert pairs[-1]["to"] == "a69"


# ── load_scans window ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["failed", "success"])
def test_run_started_before_the_window_is_not_a_scan(memdb, status: str) -> None:
    from auditor import ui

    with memdb() as s:
        s.add(AuditRun(run_id="early", pipeline="repo-audit", status=status, started_at=T0 - timedelta(hours=2),
                       metadata_json={"scanned_repos": ["x"]}))
        s.add(FindingRecord(run_id="early", repo="x", file_path="/home/smk/x/app.py", line=3,
                            finding_type="bug", severity="HIGH", title="t", source="llm",
                            created_at=T0 + timedelta(hours=1)))
    with ui._db_session() as s:
        assert tm.load_scans(s, since=T0) == []


# ── git parsing ──────────────────────────────────────────────────────────────

def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "x"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    return repo


def test_commit_messages_with_unit_separator_and_latin1_are_kept(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "fix: sep\x1fin message\n\nPack: pack-x-0123456789")
    msg = tmp_path / "msg"
    msg.write_bytes(b"fix: caf\xe9 latin-1\n")
    _git(repo, "-c", "i18n.commitEncoding=latin-1", "commit", "-q", "--allow-empty", "-F", str(msg))
    log = tm.mirror_git_log({"x": repo})("x", "2000-01-01")
    assert len(log) == 2
    assert any("Pack: pack-x-0123456789" in m for _s, _d, m in log)
    commits = tm.MirrorCommits(repo).commits("2000-01-01")
    assert len(commits) == 2
    assert any("Pack: pack-x-0123456789" in c["message"] for c in commits)


def test_octopus_merge_attributes_every_merged_parent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    heads = []
    for b in ("b1", "b2"):
        _git(repo, "checkout", "-q", "-b", b, "dev")
        _git(repo, "commit", "-q", "--allow-empty", "-m", b)
        heads.append(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "dev")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "dev work")
    _git(repo, "merge", "-q", "--no-edit", "b1", "b2")
    mc = tm.MirrorCommits(repo)
    merge = next(c for c in mc.commits("2000-01-01") if len(c["parents"]) == 3)
    assert set(mc.merged_by(merge)) == set(heads)


def test_history_file_round_trip_is_unchanged(tmp_path: Path) -> None:
    lines = [json.dumps({"at": f"2026-11-0{i}"}) for i in range(1, 4)]
    out = _history(tmp_path, ("\n".join(lines) + "\n").encode())
    assert [e["at"] for e in tm.load_history(out)] == ["2026-11-01", "2026-11-02", "2026-11-03"]
