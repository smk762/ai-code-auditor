from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from auditor.config import RepoConfig
from auditor.contracts import BranchDiff, ScanContext
from auditor.repo_resolver import BRANCH_PREFERENCE, detect_checked_out_branch, diff_files, select_branch
from auditor.repo_scanner import scan_repo_with_context


def _make_local_git_repo(tmp_path: Path, branches: list[str]) -> Path:
    """Init a bare-minimum git repo with the given local branches."""
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, capture_output=True)
    # Need at least one commit before branches can be created.
    (tmp_path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True, capture_output=True)
    # Rename the default branch to the first requested one.
    subprocess.run(["git", "-C", str(tmp_path), "branch", "-M", branches[0]], check=True, capture_output=True)
    for extra in branches[1:]:
        subprocess.run(["git", "-C", str(tmp_path), "branch", extra], check=True, capture_output=True)
    return tmp_path


def test_explicit_branch_is_returned_unchanged(tmp_path: Path) -> None:
    repo = RepoConfig(name="r", path=str(tmp_path), branch="feature/x")
    assert select_branch(repo) == "feature/x"


def test_auto_detects_dev_when_present(tmp_path: Path) -> None:
    _make_local_git_repo(tmp_path, ["dev", "main"])
    repo = RepoConfig(name="r", path=str(tmp_path))
    assert select_branch(repo, tmp_path) == "dev"


def test_falls_back_to_main_when_no_dev(tmp_path: Path) -> None:
    _make_local_git_repo(tmp_path, ["main"])
    repo = RepoConfig(name="r", path=str(tmp_path))
    assert select_branch(repo, tmp_path) == "main"


def test_falls_back_to_master_when_only_master(tmp_path: Path) -> None:
    _make_local_git_repo(tmp_path, ["master"])
    repo = RepoConfig(name="r", path=str(tmp_path))
    assert select_branch(repo, tmp_path) == "master"


def test_falls_back_to_first_preference_when_no_git_repo(tmp_path: Path) -> None:
    """Non-git directory (e.g. a bare mount) should return the first preference."""
    repo = RepoConfig(name="r", path=str(tmp_path))
    assert select_branch(repo, tmp_path) == BRANCH_PREFERENCE[0]


def test_branch_preference_order() -> None:
    assert BRANCH_PREFERENCE[0] == "dev"
    assert "main" in BRANCH_PREFERENCE
    assert "master" in BRANCH_PREFERENCE


# ---------------------------------------------------------------------------
# detect_checked_out_branch
# ---------------------------------------------------------------------------

def test_detect_checked_out_branch_returns_current(tmp_path: Path) -> None:
    _make_local_git_repo(tmp_path, ["main"])
    assert detect_checked_out_branch(tmp_path) == "main"


def test_detect_checked_out_branch_non_git(tmp_path: Path) -> None:
    assert detect_checked_out_branch(tmp_path) == ""


# ---------------------------------------------------------------------------
# diff_files
# ---------------------------------------------------------------------------

def _make_repo_with_feature_branch(tmp_path: Path) -> Path:
    """dev branch has one file; feature branch adds another."""
    _make_local_git_repo(tmp_path, ["dev"])
    # Add a file only on the feature branch.
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-b", "feature/x"], check=True, capture_output=True)
    (tmp_path / "new_feature.py").write_text("x = 1")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "add feature"], check=True, capture_output=True)
    return tmp_path


def test_diff_files_detects_new_file_on_feature_branch(tmp_path: Path) -> None:
    repo_path = _make_repo_with_feature_branch(tmp_path)
    changed = diff_files(repo_path, base="dev", head="feature/x")
    assert "new_feature.py" in changed


def test_diff_files_returns_empty_for_identical_refs(tmp_path: Path) -> None:
    _make_local_git_repo(tmp_path, ["dev"])
    changed = diff_files(tmp_path, base="dev", head="dev")
    assert changed == frozenset()


def test_diff_files_returns_empty_for_non_git_dir(tmp_path: Path) -> None:
    assert diff_files(tmp_path, base="dev") == frozenset()


# ---------------------------------------------------------------------------
# scan_repo_with_context
# ---------------------------------------------------------------------------

def test_scan_context_feature_branch_diff_present(tmp_path: Path) -> None:
    """When checked-out branch ≠ dev, context contains a feature→dev diff."""
    (tmp_path / "app.py").write_text("x = 1")
    repo_path = _make_repo_with_feature_branch(tmp_path)
    repo = RepoConfig(name="r", path=str(repo_path))
    ctx = scan_repo_with_context(repo)

    assert ctx.checked_out_branch == "feature/x"
    assert len(ctx.diffs) >= 1
    feature_diff = next(d for d in ctx.diffs if d.head == "feature/x")
    assert feature_diff.base == "dev"
    assert "new_feature.py" in feature_diff.changed_files


def test_scan_context_on_integration_branch_no_feature_diff(tmp_path: Path) -> None:
    """When checked-out branch IS dev, no feature→dev diff is generated."""
    _make_local_git_repo(tmp_path, ["dev", "main"])
    repo = RepoConfig(name="r", path=str(tmp_path))
    ctx = scan_repo_with_context(repo)

    assert ctx.checked_out_branch == "dev"
    # Should not have a diff where head == "dev" against itself.
    assert not any(d.head == d.base for d in ctx.diffs)


def test_scan_context_dev_to_main_diff_present(tmp_path: Path) -> None:
    """When dev and main both exist, context includes a dev→main diff."""
    _make_local_git_repo(tmp_path, ["main"])
    # Create dev off main and add a file.
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-b", "dev"], check=True, capture_output=True)
    (tmp_path / "dev_only.py").write_text("y = 2")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "dev work"], check=True, capture_output=True)

    repo = RepoConfig(name="r", path=str(tmp_path))
    ctx = scan_repo_with_context(repo)

    dev_main_diff = next((d for d in ctx.diffs if d.base == "main" and d.head == "dev"), None)
    assert dev_main_diff is not None
    assert "dev_only.py" in dev_main_diff.changed_files


def test_scan_context_missing_path_returns_empty(tmp_path: Path) -> None:
    repo = RepoConfig(name="r", path=str(tmp_path / "not_here"))
    ctx = scan_repo_with_context(repo)
    assert ctx.files == []
    assert ctx.diffs == []
