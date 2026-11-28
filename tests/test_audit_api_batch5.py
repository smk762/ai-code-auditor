"""validate_patch's lazily exported trees: a failed export is never reused half-written (batch 5)."""
from __future__ import annotations

from pathlib import Path

from auditor import audit_api


def test_a_failed_export_is_removed_and_never_returned_later(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def half_export(repo_path, dest, base=None):
        calls.append(dest)
        dest.mkdir(parents=True)
        (dest / "partial.py").write_text("x = 1\n")
        raise RuntimeError("git archive died mid-stream")

    monkeypatch.setattr(audit_api, "_export_clean_tree", half_export)
    tree = audit_api._lazy_clean_tree(tmp_path / "repo", tmp_path / "base", None, what="base", repo="r")
    assert tree() is None
    assert not (tmp_path / "base").exists()
    assert tree() is None  # not the half-written directory, and no second export
    assert len(calls) == 1


def test_a_good_export_runs_once(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def export(repo_path, dest, base=None):
        calls.append(dest)
        dest.mkdir(parents=True)
        return "dev"

    monkeypatch.setattr(audit_api, "_export_clean_tree", export)
    tree = audit_api._lazy_clean_tree(tmp_path / "repo", tmp_path / "base", None, what="base", repo="r")
    assert tree() == tmp_path / "base"
    assert tree() == tmp_path / "base"
    assert len(calls) == 1
