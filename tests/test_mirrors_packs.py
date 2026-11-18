"""conductor.mirrors: an empty path never means the CWD; clone URLs can't be git options or odd transports."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import conductor.mirrors as cm


def test_empty_path_never_touches_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    touched: list[Any] = []
    monkeypatch.setattr(cm, "checkout_scan_branch", lambda *a, **kw: touched.append(a) or "dev")
    for item in ({"name": "m", "path": ""}, {"name": "m"}, {"name": "m", "path": "  "}):
        res = cm._sync_mirror_item(item, preferred="dev")
        assert not res.ok and res.action == "skip"
    assert touched == []


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: Any, *a: Any, **kw: Any) -> Any:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 128, "", "nope")

    monkeypatch.setattr(cm.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize("url", [
    "--upload-pack=touch /tmp/pwned",
    "ext::sh -c touch% /tmp/pwned",
    "file:///etc",
    "/home/someone/private-repo",
])
def test_unsafe_clone_urls_are_refused(url: str, tmp_path: Path, git_calls: list[list[str]]) -> None:
    res = cm._sync_mirror_item({"name": "m", "path": str(tmp_path / "m"), "url": url}, preferred="dev")
    assert not res.ok
    assert not any("clone" in c for c in git_calls)


def test_https_clone_puts_url_after_end_of_options(tmp_path: Path, git_calls: list[list[str]]) -> None:
    url = "https://github.com/o/r.git"
    cm._sync_mirror_item({"name": "m", "path": str(tmp_path / "m"), "url": url}, preferred="dev")
    clones = [c for c in git_calls if "clone" in c]
    assert clones
    for c in clones:
        assert c.index("--") == c.index(url) - 1
