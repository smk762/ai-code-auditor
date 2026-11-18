from __future__ import annotations

import logging
from pathlib import Path

import pytest

from auditor.config import RepoConfig
from auditor.repo_scanner import scan_repo


def test_scan_repo_returns_empty_for_missing_path(tmp_path: Path) -> None:
    """Missing mount / unmounted NFS path must not crash — returns empty list."""
    repo = RepoConfig(name="unmounted", path=str(tmp_path / "not_here"))
    result = scan_repo(repo)
    assert result == []


def test_scan_repo_emits_warning_for_missing_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = RepoConfig(name="ghost-mount", path=str(tmp_path / "not_here"))
    with caplog.at_level(logging.WARNING, logger="auditor.repo_scanner"):
        scan_repo(repo)
    assert any("ghost-mount" in r.message and "skipping" in r.message for r in caplog.records)


def test_scan_repo_finds_source_files(tmp_path: Path) -> None:
    """Sanity-check: scanner picks up known extensions and ignores vendor dirs."""
    (tmp_path / "app.py").write_text("x = 1")
    (tmp_path / "lib.js").write_text("const x = 1;")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.ts").write_text("export {};")

    repo = RepoConfig(name="local-test", path=str(tmp_path))
    files = scan_repo(repo)
    names = {f.name for f in files}
    assert "app.py" in names
    assert "lib.js" in names
    assert "dep.ts" not in names


def test_scan_repo_respects_subpath(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("pass")
    (tmp_path / "other" ).mkdir()
    (tmp_path / "other" / "skip.py").write_text("pass")

    repo = RepoConfig(name="sub", path=str(tmp_path), subpath="src")
    files = scan_repo(repo)
    names = {f.name for f in files}
    assert "main.py" in names
    assert "skip.py" not in names


def test_scan_repo_under_an_ignored_ancestor_dir_still_finds_files(tmp_path: Path) -> None:
    root = tmp_path / "build" / "mirror"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "app.py").write_text("x = 1")
    (root / "dist").mkdir()
    (root / "dist" / "bundle.js").write_text("x")
    files = scan_repo(RepoConfig(name="under-build", path=str(root)))
    assert [f.name for f in files] == ["app.py"]


def test_scan_repo_skips_symlinks_that_leave_the_repo(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("TOKEN")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "real.py").write_text("x = 1")
    (root / "leak.py").symlink_to(outside)
    (root / "alias.py").symlink_to(root / "real.py")  # stays inside: kept
    names = {f.name for f in scan_repo(RepoConfig(name="links", path=str(root)))}
    assert names == {"real.py", "alias.py"}


def _fake_git_repo(tmp_path: Path, monkeypatch, checked_out: str, branches: set[str]):
    import auditor.repo_scanner as rs

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(rs, "scan_repo", lambda repo: [])
    monkeypatch.setattr(rs, "resolve_repo_path", lambda repo: tmp_path)
    monkeypatch.setattr(rs, "detect_checked_out_branch", lambda p: checked_out)
    monkeypatch.setattr(rs, "select_branch", lambda repo, p: "dev")
    monkeypatch.setattr(rs, "branch_exists", lambda p, b: b in branches)
    monkeypatch.setattr(rs, "diff_files", lambda p, base, head: frozenset({"f.py"}))
    return rs.scan_repo_with_context(RepoConfig(name="r", path=str(tmp_path)))


@pytest.mark.parametrize("checked_out", ["main", "master"])
def test_production_checkout_is_not_a_feature_branch(tmp_path: Path, monkeypatch, checked_out) -> None:
    ctx = _fake_git_repo(tmp_path, monkeypatch, checked_out, {"dev", checked_out})
    assert [(d.base, d.head) for d in ctx.diffs] == [(checked_out, "dev")]


def test_feature_checkout_still_diffs_against_dev(tmp_path: Path, monkeypatch) -> None:
    ctx = _fake_git_repo(tmp_path, monkeypatch, "feat/x", {"dev", "main"})
    assert [(d.base, d.head) for d in ctx.diffs] == [("dev", "feat/x"), ("main", "dev")]
