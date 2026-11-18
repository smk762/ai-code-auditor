"""Regression tests for auditor/diff_auditor.py path matching and containment."""
from __future__ import annotations

from pathlib import Path

from auditor import diff_auditor as da
from auditor.config import RepoConfig
from auditor.contracts import CodeUnit, DiffHunk, FileDiff


def _unit(path: str, start: int, end: int) -> CodeUnit:
    return CodeUnit(id=f"{path}:{start}", repo="r", file_path=path, symbol="f", kind="function",
                    language="python", start_line=start, end_line=end)


def _fd(path: str, new_start: int, new_count: int) -> FileDiff:
    h = DiffHunk(file_path=path, old_start=new_start, old_count=new_count, new_start=new_start,
                 new_count=new_count, raw_hunk="", added_lines=[], removed_lines=[])
    return FileDiff(old_path=path, new_path=path, hunks=[h], is_new_file=False, is_deleted=False,
                    is_rename=False)


def test_match_path_prefers_the_most_specific_candidate():
    candidates = {"utils.py", "pkg/utils.py"}
    for _ in range(20):  # order of a set must not matter
        assert da._match_path("/repo/pkg/utils.py", set(candidates)) == "pkg/utils.py"
    assert da._match_path("/repo/utils.py", candidates) == "utils.py"


def test_filter_uses_repo_relative_exact_match(tmp_path):
    root = tmp_path / "repo"
    diffs = [_fd("utils.py", 10, 2), _fd("pkg/utils.py", 50, 2)]
    units = [
        _unit(str(root / "pkg" / "utils.py"), 48, 55),   # overlaps pkg/utils.py hunk
        _unit(str(root / "pkg" / "utils.py"), 5, 12),    # only overlaps utils.py's hunk range
        _unit(str(root / "other" / "utils.py"), 1, 3),   # importer, not in the diff: kept whole
    ]
    kept = {u.id for u in da._filter_units_to_hunks(units, diffs, repo_root=root)}
    assert kept == {f"{root}/pkg/utils.py:48", f"{root}/other/utils.py:1"}


def test_diff_paths_outside_the_repo_are_never_read(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ok.py").write_text("def f():\n    return 1\n")
    secret = tmp_path / "secret.py"
    secret.write_text("def leak():\n    return 'TOKEN'\n")
    diff = (
        "diff --git a/ok.py b/ok.py\n--- a/ok.py\n+++ b/ok.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
        "diff --git a/../secret.py b/../secret.py\n--- a/../secret.py\n+++ b/../secret.py\n@@ -1,2 +1,2 @@\n def leak():\n-    x\n+    return 'TOKEN'\n"
        f"--- {secret}\n+++ {secret}\n@@ -1,2 +1,2 @@\n def leak():\n-    x\n+    return 'TOKEN'\n"
    )
    seen_impact: list[list[str]] = []
    seen_static: list[list[str]] = []
    analyzed: list[str] = []

    def fake_impact(repo_path, changed):
        seen_impact.append(list(changed))
        from auditor.contracts import ImpactResult
        return ImpactResult(directly_changed=frozenset(changed), transitively_affected=frozenset())

    class FakeLLM:
        def __init__(self, **kw):
            pass

        def analyze_code(self, unit, repo_path=None):
            analyzed.append(unit.file_path)
            return []

    monkeypatch.setattr(da, "resolve_repo_path", lambda repo: root)
    monkeypatch.setattr(da, "analyze_impact", fake_impact)
    monkeypatch.setattr(da, "build_repo_briefing", lambda *a, **k: "")
    monkeypatch.setattr(da, "LLMClient", FakeLLM)
    monkeypatch.setattr(da, "run_static_checks_on_files",
                        lambda name, files, root=None: seen_static.append(list(files)) or [])

    repo = RepoConfig(name="r", path=str(root))
    da.run_diff_audit(repo, diff)
    assert seen_impact == [["ok.py"]]
    assert seen_static == [[str(root / "ok.py")]]
    assert analyzed and all(Path(p).resolve().is_relative_to(root.resolve()) for p in analyzed)
