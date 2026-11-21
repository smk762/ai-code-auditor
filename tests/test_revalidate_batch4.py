"""revalidate: rebase merges, the fix_patch contract, malformed packs, since filtering (batch 4)."""
from __future__ import annotations

from pathlib import Path

import yaml

from auditor import revalidate as rv
from tests.test_revalidate import _commit, _git, _pack, _repo


# --- cc69cbad7b: a rebase-merged PR's patch covers all of its commits ----------------------------

def _rebase_merged(tmp_path: Path, *, squash_subject: bool = False):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _commit(repo, {"app/calc.py": "def add(a, b):\n    return a - b\n", "app/other.py": "X = 1\n"}, "init")
    base = _git(repo, "rev-parse", "HEAD")
    # What GitHub's "rebase and merge" leaves on dev: the PR's commits replayed in a line.
    c1 = _commit(repo, {"app/calc.py": "def add(a, b):\n    return a + b\n"}, "fix calc")
    c2 = _commit(repo, {"tests/test_calc.py": "def test_add():\n    pass\n"},
                 "Fix calc (#7)" if squash_subject else "add test")
    pr = {"merge_sha": c2, "commit_shas": ["a" * 40, "b" * 40], "number": 7, "url": "u",
          "pack_ids": ["pack-a"], "merged_at": "2026-11-17T10:00:00Z"}
    return repo, pr, base, c1


def test_rebase_merge_takes_the_base_before_every_pr_commit(tmp_path) -> None:
    repo, pr, base, _ = _rebase_merged(tmp_path)
    got_base, patch, files = rv.fix_patch(repo / ".git", pr, "app/calc.py")
    assert got_base == base
    assert "+    return a + b" in patch
    # The original shas aren't in the cache; the rebased commits that touched the file are used.
    assert files == ["app/calc.py"]


def test_squash_subject_keeps_the_first_parent_base(tmp_path) -> None:
    repo, pr, _, c1 = _rebase_merged(tmp_path, squash_subject=True)
    got_base, _, _ = rv.fix_patch(repo / ".git", pr, "tests/test_calc.py")
    assert got_base == c1


# --- 02f35c92bf: a PR that doesn't touch the path returns ("", "", []) ---------------------------

def test_untouched_path_returns_the_documented_empty_triple(tmp_path) -> None:
    repo, pr, _ = _repo(tmp_path)
    assert rv.fix_patch(repo / ".git", pr, "app/missing.py") == ("", "", [])


# --- 2569495239 / 082c040f53: one bad pack never aborts the scan ---------------------------------

def test_an_undecodable_pack_is_skipped(tmp_path) -> None:
    out = tmp_path / "reports"
    _pack(out, "pack-good", "unverified")
    (out / "agent_packs" / "pack-latin1.yaml").write_bytes(b"status: unverified\nnote: caf\xe9\n")
    assert [c.pack_id for c in rv.candidates(out)] == ["pack-good"]


def test_non_mapping_packs_and_fields_are_skipped(tmp_path) -> None:
    out = tmp_path / "reports"
    _pack(out, "pack-good", "unverified")
    d = out / "agent_packs"
    (d / "pack-list.yaml").write_text(yaml.safe_dump(["a", "b"]))
    (d / "pack-scalar.yaml").write_text("just a string\n")
    (d / "pack-nested.yaml").write_text(yaml.safe_dump({
        "work_item_id": "pack-nested", "kind": "nightly", "status": "unverified", "repo": "r",
        "verification": "x", "location": {"file_path": "/m/r/app/calc.py"},
        "status_history": ["oops", {"at": "2026-11-01T00:00:00Z"}]}))
    got = {c.pack_id: c for c in rv.candidates(out)}
    assert set(got) == {"pack-good", "pack-nested"}
    assert got["pack-nested"].since == "2026-11-01T00:00:00Z"


# --- 5330583458: since honours its offset and separator ------------------------------------------

def _prs():
    return [{"repo": "r", "merge_sha": "m1", "url": "early", "merged_at": "2026-11-17T09:30:00Z"},
            {"repo": "r", "merge_sha": "m2", "url": "late", "merged_at": "2026-11-17T10:30:00Z"}]


def test_since_with_an_offset_is_compared_in_utc() -> None:
    # 12:00+02:00 is 10:00Z: only the 10:30Z PR follows it.
    c = rv.Candidate("p", "r", "app/calc.py", "unverified", "", since="2026-11-17T12:00:00+02:00")
    assert [pr["url"] for _, pr in rv.prs_for(c, {}, _prs())] == ["late"]
    c.since = "2026-11-17T08:00:00-02:00"  # 10:00Z
    assert [pr["url"] for _, pr in rv.prs_for(c, {}, _prs())] == ["late"]


def test_since_with_a_space_separator() -> None:
    c = rv.Candidate("p", "r", "app/calc.py", "unverified", "", since="2026-11-17 10:00:00+00:00")
    assert [pr["url"] for _, pr in rv.prs_for(c, {}, _prs())] == ["late"]
