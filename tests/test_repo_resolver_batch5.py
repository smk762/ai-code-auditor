"""Regression tests for auditor/repo_resolver.py audit packs (batch 5)."""
from __future__ import annotations

import pytest

from auditor.config import RepoConfig


@pytest.mark.parametrize("name", ["-u", "--upload-pack=touch_pwned", ".", "..", ".hidden"])
def test_a_cache_name_must_start_with_a_letter_or_digit(name: str) -> None:  # a98e6835f2
    from auditor.repo_resolver import _cache_dir

    # With a cache path of ".", "-u" would be the clone directory itself, read by git as an option.
    repo = RepoConfig(name=name, path="https://github.com/o/r.git", provider="github", local_cache_path=".")
    with pytest.raises(ValueError, match="not a safe cache directory name"):
        _cache_dir(repo)


def test_an_ordinary_name_is_a_direct_child_of_the_cache(tmp_path) -> None:
    from auditor.repo_resolver import _cache_dir

    root, repo_dir = _cache_dir(RepoConfig(name="argus-x_1.2", path="https://github.com/o/r.git",
                                           local_cache_path=str(tmp_path)))
    assert repo_dir.parent == root and repo_dir.name == "argus-x_1.2"
