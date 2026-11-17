"""ai-audit revalidate: finding a merged fix's patch and base, and which packs it re-checks."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from auditor import revalidate as rv


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for k, v in files.items():
        (repo / k).parent.mkdir(parents=True, exist_ok=True)
        (repo / k).write_text(v)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _commit(repo, {"app/calc.py": "def add(a, b):\n    return a - b\n", "app/other.py": "X = 1\n"}, "init")
    _git(repo, "checkout", "-qb", "fix")
    fix = _commit(repo, {"app/calc.py": "def add(a, b):\n    return a + b\n",
                         "tests/test_calc.py": "def test_add():\n    pass\n"}, "fix calc")
    other = _commit(repo, {"app/other.py": "X = 2\n"}, "other pack")
    _git(repo, "checkout", "-q", "dev")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "-q", "--no-ff", "fix", "-m", "Merge fix")
    return repo, {"merge_sha": _git(repo, "rev-parse", "HEAD"), "commit_shas": [fix, other],
                  "url": "u", "pack_ids": ["pack-a"], "merged_at": "2026-11-17T10:00:00Z"}, base


def test_fix_patch_takes_the_files_of_the_commits_that_touched_the_pack(tmp_path) -> None:
    repo, pr, base = _repo(tmp_path)
    got_base, patch, files = rv.fix_patch(repo / ".git", pr, "app/calc.py")
    assert got_base == base and files == ["app/calc.py", "tests/test_calc.py"]
    assert "+    return a + b" in patch and "other.py" not in patch
    assert rv.fix_patch(repo / ".git", pr, "app/missing.py")[1] == ""
    assert rv.fix_patch(repo / ".git", {**pr, "merge_sha": "0" * 40}, "app/calc.py") == ("", "", [])


def _pack(out: Path, pid: str, status: str, kind: str = "", repo: str = "r", ptype: str = "nightly") -> None:
    d = {"work_item_id": pid, "kind": ptype, "status": status, "repo": {"name": repo},
         "location": {"file_path": f"/m/{repo}/app/calc.py"},
         "verification": {"evidence": {"kind": kind}} if kind else {}}
    (out / "agent_packs").mkdir(parents=True, exist_ok=True)
    (out / "agent_packs" / f"{pid}.yaml").write_text(yaml.safe_dump(d))


def test_run_picks_waived_and_unverified_packs_and_reports_each(tmp_path) -> None:
    repo, pr, base = _repo(tmp_path)
    out = tmp_path / "reports"
    _pack(out, "pack-a", "resolved", "waiver")
    _pack(out, "pack-b", "unverified")
    _pack(out, "pack-c", "resolved", "repro")  # proven already: not a candidate
    _pack(out, "pack-d", "open")
    _pack(out, "pack-e", "resolved", "waiver", ptype="architecture")
    (out / "trust_prs.json").write_text(__import__("json").dumps({"prs": [pr]}))
    sent = []

    def post(body):
        sent.append(body)
        return {"passed": True, "tool": "pytest", "pack": {"updated": True, "evidence": "repro", "reason": "1 test"}}

    rep = rv.run(out, post=post, git_dirs={"r": repo / ".git"})
    assert rep["candidates"] == 2 and rep["counts"] == {"upgraded": 1, "no_pr": 1}
    [body] = sent
    assert body["pack_id"] == "pack-a" and body["revalidate"] and body["base_ref"] == base
    assert (out / rv.REPORT_NAME).exists()
    assert rv.run(out, post=post, git_dirs={"r": repo / ".git"}, dry_run=True)["counts"] == {"unchanged": 1, "no_pr": 1}


def test_a_pr_without_pack_ids_matches_by_file_when_it_merged_after_the_work_began(tmp_path) -> None:
    repo, pr, base = _repo(tmp_path)
    anon = {**pr, "pack_ids": [], "repo": "r"}
    c = rv.Candidate("pack-a", "r", "app/calc.py", "resolved", "waiver", since="2026-11-17T08:00:00+00:00")
    [(how, got)] = rv.prs_for(c, rv.prs_by_pack([anon]), [anon])
    assert how == "file" and got is anon
    assert rv.prs_for(rv.Candidate("pack-a", "r", "app/calc.py", "resolved", "waiver",
                                   since="2026-11-18T00:00:00+00:00"), {}, [anon]) == []  # merged before
    o = rv.revalidate_one(c, [("file", anon)], repo / ".git", lambda b: {}, dry_run=True)
    assert (o.result, o.matched, o.base) == ("unchanged", "file", base)


def test_retry_days_skips_packs_sent_recently_with_nothing_to_show(tmp_path) -> None:
    repo, pr, _ = _repo(tmp_path)
    out = tmp_path / "reports"
    _pack(out, "pack-a", "resolved", "waiver")
    (out / "trust_prs.json").write_text(__import__("json").dumps({"prs": [pr]}))
    calls = []

    def post(body):
        calls.append(body)
        return {"passed": True, "tool": "lint_only", "pack": {"updated": False, "reason": "no stronger evidence"}}

    kw = {"post": post, "git_dirs": {"r": repo / ".git"}}
    assert rv.run(out, retry_days=7, **kw)["counts"] == {"unchanged": 1}
    again = rv.run(out, retry_days=7, **kw)
    assert again["skipped_recent"] == 1 and again["candidates"] == 0 and len(calls) == 1
    assert rv.run(out, **kw)["counts"] == {"unchanged": 1} and len(calls) == 2  # by hand: no wait


def test_poster_waits_out_busy_slots_then_gives_up(monkeypatch) -> None:
    import io
    import urllib.error

    calls, slept = [], []

    def busy(code=429):
        return urllib.error.HTTPError("u", code, "busy", {"Retry-After": "30"}, io.BytesIO(b'{"detail":"busy"}'))

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    replies = [busy(), busy(), _Resp(b'{"passed": true}')]

    def urlopen(req, timeout):
        calls.append(req)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(rv.urllib.request, "urlopen", urlopen)
    post = rv.api_poster("http://x", busy_wait_s=100, sleep=slept.append)
    assert post({}) == {"passed": True} and slept == [30.0, 30.0] and len(calls) == 3

    replies[:] = [busy()] * 10
    slept.clear()
    try:
        rv.api_poster("http://x", busy_wait_s=45, sleep=slept.append)({})
        raise AssertionError("should give up")
    except urllib.error.HTTPError as exc:
        assert exc.code == 429 and sum(slept) == 45
    replies[:] = [busy(500)]
    try:
        rv.api_poster("http://x", busy_wait_s=100, sleep=slept.append)({})
        raise AssertionError("a 500 is not busy")
    except urllib.error.HTTPError as exc:
        assert exc.code == 500


def test_an_error_is_not_an_attempt(tmp_path) -> None:
    repo, pr, _ = _repo(tmp_path)
    out = tmp_path / "reports"
    _pack(out, "pack-a", "resolved", "waiver")
    (out / "trust_prs.json").write_text(__import__("json").dumps({"prs": [pr]}))

    def post(body):
        raise OSError("refused")

    kw = {"post": post, "git_dirs": {"r": repo / ".git"}, "retry_days": 7}
    assert rv.run(out, **kw)["counts"] == {"error": 1}
    assert rv.run(out, **kw)["skipped_recent"] == 0  # retried on the next run
