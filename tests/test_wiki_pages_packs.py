"""Regression tests for wiki/pages.py audit packs."""
from __future__ import annotations

from pathlib import Path

from auditor.config import RepoConfig
from wiki import pages


def test_readme_directory_is_skipped_for_the_readme_file(tmp_path: Path) -> None:
    (tmp_path / "README.assets").mkdir()  # sorts before README.md
    (tmp_path / "README.md").write_text("# svc\nThe real readme.\n")
    ctx = pages.repo_context(RepoConfig("svc", str(tmp_path)), [], "structure", ecosystem_text="")
    assert "## README.md" in ctx and "The real readme." in ctx


def test_only_a_readme_directory_means_no_readme(tmp_path: Path) -> None:
    (tmp_path / "README").mkdir()
    ctx = pages.repo_context(RepoConfig("svc", str(tmp_path)), [], "structure", ecosystem_text="")
    assert "## README" not in ctx and ctx.endswith("structure")
