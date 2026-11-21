"""scripts/check_branch_rules.py: the "main never ahead of dev" rule, on throwaway git repos."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_branch_rules", ROOT / "scripts" / "check_branch_rules.py")
rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rules)


def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": "/usr/bin:/bin:/usr/local/bin"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"{name}: {text}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "dev")
    commit(tmp_path, "a.txt", "1")
    git(tmp_path, "branch", "main")
    return tmp_path


def run_main_tree(repo: Path) -> int:
    return rules.main(["main-tree", "--repo", str(repo), "--dev", "dev", "--main", "main"])


@pytest.mark.parametrize("base, head, allowed", [
    ("main", "dev", True),            # the release PR
    ("dev", "feat/x", True),          # every other PR targets dev
    ("dev", "main", True),
    ("main", "feat/x", False),
    ("main", "hotfix", False),
])
def test_pr_into_main_must_come_from_dev(base: str, head: str, allowed: bool) -> None:
    assert (rules.check_pr_base(base, head) is None) is allowed
    assert rules.main(["pr-base", "--base", base, "--head", head]) == (0 if allowed else 1)


def test_main_behind_dev_passes(repo: Path) -> None:
    git(repo, "checkout", "-q", "dev")
    commit(repo, "b.txt", "unreleased work")  # dev ahead of main is the normal state
    assert run_main_tree(repo) == 0


def test_fast_forward_release_passes(repo: Path) -> None:
    commit(repo, "b.txt", "2")
    git(repo, "branch", "-f", "main", "dev")
    assert run_main_tree(repo) == 0


def test_release_merge_commit_passes_even_after_dev_moves_on(repo: Path) -> None:
    commit(repo, "b.txt", "2")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request: release", "dev")
    assert git(repo, "rev-list", "--count", "dev..main").strip() == "1"  # main one commit ahead, same tree
    assert run_main_tree(repo) == 0
    git(repo, "checkout", "-q", "dev")
    commit(repo, "c.txt", "3")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request: second release", "dev")
    assert run_main_tree(repo) == 0


def test_commit_pushed_to_main_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git(repo, "checkout", "-q", "main")
    commit(repo, "hotfix.txt", "pushed straight to main")
    assert run_main_tree(repo) == 1
    assert "hotfix.txt" in capsys.readouterr().err


def test_release_merge_carrying_a_main_only_change_fails(repo: Path) -> None:
    git(repo, "checkout", "-q", "main")
    commit(repo, "hotfix.txt", "main only")
    git(repo, "checkout", "-q", "dev")
    commit(repo, "b.txt", "2")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "release", "dev")
    assert run_main_tree(repo) == 1


def test_feature_branch_merged_into_main_fails(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feat", "dev")
    commit(repo, "f.txt", "feature")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge feat into main", "feat")
    assert run_main_tree(repo) == 1


def test_unknown_or_option_like_ref_is_a_usage_error(repo: Path) -> None:
    assert rules.main(["main-tree", "--repo", str(repo), "--dev", "dev", "--main", "nope"]) == 2
    assert rules.main(["main-tree", "--repo", str(repo), "--dev=--output=x", "--main", "main"]) == 2

