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
