from __future__ import annotations

from conductor.github_awareness import OpenItem, RepoAwareness, overlap_reason


def test_overlap_same_file():
    aw = RepoAwareness(
        repo="argus-lens",
        items=[
            OpenItem(
                kind="pr",
                number=12,
                title="Fix caption parser",
                url="https://github.com/smk762/argus-lens/pull/12",
                files=["src/argus_lens/parser.py"],
            )
        ],
    )
    reason = overlap_reason(
        title="Something else",
        file_path="src/argus_lens/parser.py",
        awareness=aw,
    )
    assert reason and "12" in reason


def test_no_overlap():
    aw = RepoAwareness(
        repo="argus-lens",
        items=[
            OpenItem(kind="issue", number=3, title="Docs typo", url="u", body="readme")
        ],
    )
    assert (
        overlap_reason(title="SQL injection in API", file_path="api/routes.py", awareness=aw)
        is None
    )


def _pr_touching(*files: str) -> RepoAwareness:
    return RepoAwareness(repo="r", items=[OpenItem(kind="pr", number=7, title="Unrelated", url="u",
                                                   files=list(files))])


def test_same_basename_in_another_directory_is_not_an_overlap():
    aw = _pr_touching("some_other_pkg/__init__.py", "lib/xutils.py")
    assert overlap_reason(title="Bug", file_path="conductor/__init__.py", awareness=aw) is None
    assert overlap_reason(title="Bug", file_path="utils.py", awareness=_pr_touching("lib/xutils.py")) is None


def test_path_suffix_and_bare_name_still_overlap():
    aw = _pr_touching("conductor/__init__.py")
    assert overlap_reason(title="Bug", file_path="/home/smk/mirrors/r/conductor/__init__.py", awareness=aw)
    assert overlap_reason(title="Bug", file_path="__init__.py", awareness=aw)
