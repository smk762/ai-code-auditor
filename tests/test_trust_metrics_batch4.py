"""Trust metrics: evidence identity, git robustness (locale, timeouts, odd paths), events, re-lands, headline."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from auditor import trust_metrics as tm

T0 = datetime(2026, 11, 1, tzinfo=UTC)
PYTEST = "validate_patch passed (repo=x, tool=pytest)"


def at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


def pack(pid: str, *, title: str, status: str = "open", history=(), detected: float = 0, run: str = "r0",
         line: int = 10) -> tm.PackView:
    return tm.pack_view({
        "work_item_id": pid, "kind": "nightly", "status": status,
        "repo": {"name": "x"}, "location": {"file_path": "/m/x/app.py", "line": line},
        "narrative": {"title": title}, "provenance": {"detected_at": at(detected), "run_id": run},
        "governance": {},
        "status_history": [{"status": s, "at": at(h), "note": n} if s else {"at": at(h), "note": n}
                           for s, h, n in history],
    })


def test_a_pack_and_its_source_finding_explain_only_one_resolution() -> None:
    a = pack("a", title="Missing timeout on HTTP call", status="resolved", history=[("resolved", 5, PYTEST)])
    b = pack("b", title="Missing timeout on HTTP request", status="resolved", history=[("resolved", 5, PYTEST)])
    new = pack("n", title="Missing timeout on HTTP call", detected=8, run="r9")
    # the scan that emitted pack n reports the finding it came from
    s = tm.Scan(run_id="r9", repo="x", at=T0 + timedelta(hours=8),
                findings=[{"path": "app.py", "title": "Missing timeout on HTTP call", "line": 10, "type": "bug"}])
    r = tm.recurrence_metric([a, b, new], [s])
    assert (r["observed"], r["regressed"]) == (2, 1)
    assert [e["pack_id"] for e in r["examples"]] == ["a"]


def test_reflog_lookup_runs_git_in_the_c_locale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    envs: list[dict | None] = []

    def fake_run(cmd, **kw):
        envs.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.setattr(tm.subprocess, "run", fake_run)
    assert tm._reflog_commit_time(tmp_path, T0) is None
    assert envs and all(e is not None and e["LC_ALL"] == "C" and e["LANGUAGE"] == "C" for e in envs)


def test_a_git_timeout_degrades_instead_of_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 60))

    monkeypatch.setattr(tm.subprocess, "run", slow)
    m = tm.MirrorCommits(tmp_path)
    assert m.ref == ""
    m.ref = "origin/dev"
    assert m.path_changes("2000-01-01") == []
    assert m.commits("2000-01-01") == []
    assert m.merged_by({"parents": ["a", "b"]}) == []


def test_path_changes_returns_real_paths_for_odd_names(tmp_path: Path) -> None:
    def git(*a: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=tmp_path, check=True,
                       capture_output=True)

    git("init", "-q", "-b", "dev")
    names = ["café.md", 'we"ird.py', "new\nline.py", "plain.py"]
    for n in names:
        (tmp_path / n).write_text("x\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "odd names")
    (tmp_path / "plain.py").write_text("y\n", encoding="utf-8")
    git("commit", "-qam", "second")
    changes = tm.MirrorCommits(tmp_path).path_changes("2000-01-01")
    assert [paths for _, paths in changes] == [set(names), {"plain.py"}]


def test_a_note_only_entry_does_not_split_a_resolution() -> None:
    p = pack("a", title="t", status="resolved",
             history=[("resolved", 1, "manual"), ("", 2, "comment only"), ("resolved", 3, "again")])
    assert len(tm.resolution_events([p])) == 1


PR = {"repo": "x", "slug": "o/x", "number": 7, "title": "fix: resolve ai-code-auditor findings",
      "merged_at": at(0), "merge_sha": "m" * 40, "commit_shas": ["c" * 40]}


def test_a_reland_is_not_a_revert() -> None:
    commits = [
        ("r1", at(24), 'Revert "fix: resolve ai-code-auditor findings (#7)"\n\nThis reverts commit ' + "d" * 40),
        ("r2", at(48), 'Revert "Revert "fix: resolve ai-code-auditor findings (#7)""\n\nThis reverts commit r1.'),
        ("r3", at(50), "Reverted the flaky part of #7"),
    ]
    assert tm.classify_followups(PR, commits)["reverts"] == ["r1", "r3"]


def test_headline_counts_open_finding_packs() -> None:
    packs = [pack("o1", title="a"), pack("o2", title="b"), pack("r", title="c", status="resolved")]
    report = tm.compute(packs, [], None, None, now=T0)
    assert report["headline"]["open_finding_packs"] == 2
