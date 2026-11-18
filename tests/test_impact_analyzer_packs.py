"""Regression tests for auditor/impact_analyzer.py graph cache and parse robustness."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from auditor import impact_analyzer as ia


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(ia, "_graph_cache", {})
    r = tmp_path / "repo"
    r.mkdir()
    (r / "base.py").write_text("X = 1\n")
    (r / "user.py").write_text("import base\n")
    _git(r, "init", "-q")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "one")
    return r


def test_uncommitted_import_change_is_seen(repo):
    assert ia.analyze_impact(repo, ["base.py"]).transitively_affected == {"user.py"}
    (repo / "other.py").write_text("import base\n")  # untracked, HEAD unchanged
    assert ia.analyze_impact(repo, ["base.py"]).transitively_affected == {"user.py", "other.py"}
    (repo / "user.py").write_text("X = 2\n")  # tracked edit removes the import
    assert ia.analyze_impact(repo, ["base.py"]).transitively_affected == {"other.py"}


def test_cache_keeps_one_entry_per_repo(repo):
    ia.analyze_impact(repo, ["base.py"])
    (repo / "user.py").write_text("import base\nY = 1\n")
    _git(repo, "commit", "-qam", "two")
    ia.analyze_impact(repo, ["base.py"])
    (repo / "user.py").write_text("import base\nY = 2\n")
    _git(repo, "commit", "-qam", "three")
    ia.analyze_impact(repo, ["base.py"])
    assert len(ia._graph_cache) == 1


def test_clean_tree_is_cached(repo, monkeypatch):
    ia.analyze_impact(repo, ["base.py"])
    calls = []
    monkeypatch.setattr(ia, "_build_import_graph", lambda p: calls.append(p) or {})
    ia.analyze_impact(repo, ["base.py"])
    assert calls == []


@pytest.mark.parametrize("exc", [ValueError, RecursionError, MemoryError])
def test_one_unparsable_file_does_not_abort_the_graph(tmp_path, monkeypatch, exc):
    (tmp_path / "base.py").write_text("X = 1\n")
    (tmp_path / "user.py").write_text("import base\n")
    (tmp_path / "bad.py").write_text("generated\n")
    real_parse = ast.parse

    def parse(text, filename="<unknown>", *a, **k):
        if filename.endswith("bad.py"):
            raise exc("boom")
        return real_parse(text, filename, *a, **k)

    monkeypatch.setattr(ia.ast, "parse", parse)
    graph = ia._build_import_graph(tmp_path)
    assert graph["bad.py"] == set()
    assert graph["user.py"] == {"base.py"}
