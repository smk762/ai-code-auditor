"""conductor.draft_pr: PR targets base_branch; the repo's own intent.md is never overwritten."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import conductor.draft_pr as dp
from auditor.git_ops import PushResult


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "intent.md").write_text("the project's own intent\n", encoding="utf-8")
    _git(r, "add", "intent.md")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "branch", "dev")
    return r


def test_draft_pr_targets_base_and_keeps_existing_intent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh_calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        if args and args[0] == "gh":
            gh_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "https://github.com/o/r/pull/7\n", "")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(dp, "_gh_available", lambda: True)
    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    pushed: dict[str, list[str]] = {}

    def push(work: Path, **kw: Any) -> PushResult:  # the commit as pushed, from the scratch clone
        pushed["changed"] = _git(work, "show", "--name-only", "--format=", "HEAD").split()
        return PushResult(pushed=True, remote="origin", branch=kw.get("branch", ""), remote_url="x")
    monkeypatch.setattr(dp, "push_repair_branch", push)

    res = dp.create_draft_pr(repo, repo_name="r", title="Fix it", body="notes", slug="fix it",
                             base_branch="dev")
    assert res.error == "", res.error
    assert res.draft is not None and res.draft.url.endswith("/pull/7")
    create = gh_calls[-1]
    assert create[create.index("--base") + 1] == "dev"
    assert (repo / "intent.md").read_text(encoding="utf-8") == "the project's own intent\n"
    changed = pushed["changed"]
    assert "intent.md" not in changed
    assert changed and changed[0].endswith("intent.md")


def _gh(monkeypatch: pytest.MonkeyPatch, gh: Any, pushed: bool = True, reason: str = "") -> list[dict]:
    calls: list[dict] = []
    real_run = subprocess.run

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        if args and args[0] == "gh":
            calls.append(kw)
            return gh(args, **kw)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(dp, "_gh_available", lambda: True)
    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    monkeypatch.setattr(dp, "push_repair_branch", lambda *a, **kw: PushResult(
        pushed=pushed, remote="origin", branch=kw.get("branch", ""), remote_url="x", skipped_reason=reason))
    return calls


def test_empty_gh_stdout_is_an_error_not_a_crash(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _gh(monkeypatch, lambda args, **kw: subprocess.CompletedProcess(args, 0, "", ""))
    res = dp.create_draft_pr(repo, repo_name="r", title="Fix it", body="notes", slug="fix it")
    assert res.draft is None and res.error


def test_skipped_push_stops_before_gh(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _gh(monkeypatch, lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "no such branch"),
                pushed=False, reason="no remote 'origin' configured")
    res = dp.create_draft_pr(repo, repo_name="r", title="Fix it", body="notes", slug="fix it")
    assert calls == []
    assert res.draft is None and "no remote 'origin' configured" in res.error and res.branch


def test_gh_has_a_timeout_and_no_prompt(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(args: Any, **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(args, kw.get("timeout") or 0)

    calls = _gh(monkeypatch, hang)
    res = dp.create_draft_pr(repo, repo_name="r", title="Fix it", body="notes", slug="fix it")
    assert res.draft is None and "timed out" in res.error
    assert calls[0].get("timeout") and calls[0]["env"]["GH_PROMPT_DISABLED"] == "1"


# ── never in the source checkout (2026-11-21: mirrors left on repair/* branches) ────────────

def _state(repo: Path) -> tuple[str, str, str, str]:
    return (_git(repo, "symbolic-ref", "-q", "HEAD"), _git(repo, "rev-parse", "HEAD"),
            _git(repo, "for-each-ref"), _git(repo, "status", "--porcelain"))


def test_draft_pr_never_writes_the_source_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub = tmp_path / "hub.git"  # stands in for GitHub
    _git(tmp_path, "init", "-q", "--bare", "-b", "dev", str(hub))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "dev")
    _git(seed, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one")
    _git(seed, "push", "-q", str(hub), "dev")
    # The hub is a local path, which push_repair_branch refuses by design; here it stands in for GitHub.
    import auditor.git_ops as go
    monkeypatch.setattr(go, "_is_local_path_remote", lambda url: False)
    mirror = tmp_path / "mirror"
    _git(tmp_path, "clone", "-q", str(hub), str(mirror))
    _git(mirror, "config", "user.email", "t@example.com")
    _git(mirror, "config", "user.name", "t")
    # dev moves on upstream after the mirror's checkout: the PR must start from origin/dev.
    _git(seed, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "two")
    _git(seed, "push", "-q", str(hub), "dev")
    _git(mirror, "fetch", "-q")
    before = _state(mirror)

    real_run = subprocess.run
    gh_cwd: list[str] = []

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        if args and args[0] == "gh":
            gh_cwd.append(kw.get("cwd", ""))
            return subprocess.CompletedProcess(args, 0, "https://github.com/o/r/pull/9\n", "")
        return real_run(args, *a, **kw)
    monkeypatch.setattr(dp, "_gh_available", lambda: True)
    monkeypatch.setattr(dp.subprocess, "run", fake_run)

    res = dp.create_draft_pr(mirror, repo_name="r", title="Fix it", body="notes", slug="fix it", base_branch="dev")
    assert res.error == "", res.error
    assert _state(mirror) == before  # same branch, commit, refs and clean tree
    pushed = _git(hub, "rev-parse", f"refs/heads/{res.branch}").strip()
    assert _git(hub, "rev-parse", f"{pushed}^").strip() == _git(hub, "rev-parse", "dev").strip()
    assert gh_cwd and not Path(gh_cwd[0]).exists()  # the scratch clone is gone


def test_git_writes_refuse_a_scan_source(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import auditor.git_ops as go

    monkeypatch.setattr(go, "scan_source_paths", lambda: {repo.resolve()})
    before = _state(repo)
    for call in (lambda: go.create_repair_branch(repo, "x"),
                 lambda: go.commit_repair(repo, "m", paths=["intent.md"]),
                 lambda: go.push_repair_branch(repo)):
        with pytest.raises(go.ScanSourceError):
            call()
    assert _state(repo) == before


def test_every_configured_mirror_and_repo_path_is_a_scan_source() -> None:
    import yaml

    from auditor.fsutil import host_path
    from auditor.git_ops import scan_source_paths

    paths = scan_source_paths()
    mirrors = yaml.safe_load(Path("config/repo_mirrors.yaml").read_text())["mirrors"]
    repos = yaml.safe_load(Path("config/ecosystem.yaml").read_text())["repos"]
    assert mirrors and repos
    for item in [*mirrors, *repos]:
        assert Path(host_path(item["path"])).resolve() in paths, item["path"]


# ── no orphan branches on the remote (gh missing, or the PR could not be opened) ────────────

def _hub_and_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    hub = tmp_path / "hub.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "dev", str(hub))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "dev")
    _git(seed, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one")
    _git(seed, "push", "-q", str(hub), "dev")
    import auditor.git_ops as go
    monkeypatch.setattr(go, "_is_local_path_remote", lambda url: False)
    mirror = tmp_path / "mirror"
    _git(tmp_path, "clone", "-q", str(hub), str(mirror))
    _git(mirror, "config", "user.email", "t@example.com")
    _git(mirror, "config", "user.name", "t")
    return hub, mirror


def test_missing_gh_pushes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub, mirror = _hub_and_mirror(tmp_path, monkeypatch)
    before = _git(hub, "for-each-ref")
    monkeypatch.setattr(dp, "_gh_available", lambda: False)
    res = dp.create_draft_pr(mirror, repo_name="r", title="Fix it", body="notes", slug="fix it", base_branch="dev")
    assert res.draft is None and "gh CLI not available" in res.error
    assert _git(hub, "for-each-ref") == before


@pytest.mark.parametrize("gh", [
    lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "GraphQL: something went wrong"),
    lambda args, **kw: subprocess.CompletedProcess(args, 0, "", ""),
])
def test_a_failed_pr_create_deletes_the_pushed_branch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: Any) -> None:
    hub, mirror = _hub_and_mirror(tmp_path, monkeypatch)
    before = _git(hub, "for-each-ref")
    real_run = subprocess.run

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        if args and args[0] == "gh":
            return gh(args, **kw)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(dp, "_gh_available", lambda: True)
    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    res = dp.create_draft_pr(mirror, repo_name="r", title="Fix it", body="notes", slug="fix it", base_branch="dev")
    assert res.draft is None and res.error and res.branch
    assert _git(hub, "for-each-ref") == before
