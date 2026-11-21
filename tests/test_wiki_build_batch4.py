"""wiki/build.py: _write_repo returns only the pages this call wrote (batch-4 pack)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from wiki import build as build_mod
from wiki import pages as pages_mod
from wiki.batch import Result


def test_write_repo_returns_this_calls_pages_not_the_shared_results(tmp_path: Path) -> None:
    page = next(iter(pages_mod.PAGES))
    report = build_mod.BuildReport(repos={"svc": build_mod.RepoResult("svc")})
    c = {"repo": SimpleNamespace(path=str(tmp_path)), "root": tmp_path, "verify": {page: "v1"}, "requested": {page}}
    checked = {"v1": Result("v1", text="# Page\n\nPlain text.\n", stop_reason="end_turn")}
    assert build_mod._write_repo("svc", c, checked, report, tmp_path) == [page]
    # a second call for the same repo (e.g. the other variant) reports only what it wrote
    assert build_mod._write_repo("svc", c, checked, report, tmp_path, public=True) == [page]
    assert build_mod._write_repo("svc", c | {"requested": set()}, checked, report, tmp_path) == []
    assert report.repos["svc"].pages_written == [page, page]  # the per-repo report still totals both
