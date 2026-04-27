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
