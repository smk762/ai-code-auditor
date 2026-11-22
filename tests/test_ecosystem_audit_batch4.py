"""Resumed ecosystem runs keep earlier repos; cleanup never masks the original failure."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import pipelines.ecosystem_audit as eco


def test_resumed_run_keeps_repos_finished_before_the_resume(tmp_path, monkeypatch) -> None:
    from auditor.runtime import RunMetadata

    for name in ("done", "todo"):
        (tmp_path / name / ".git").mkdir(parents=True)
    files = {n: [str(tmp_path / n / "a.py")] for n in ("done", "todo")}
    monkeypatch.setattr(eco, "scan_repo", lambda repo: files[repo.name])
    monkeypatch.setattr(eco, "extract_code_units", lambda repo_name, fp: [f"{repo_name}:a"])
    monkeypatch.setattr(eco, "upsert_repo_run", lambda *a, **k: None)
    monkeypatch.setattr(eco, "write_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(eco, "record_run_progress", lambda meta: None)

    meta = RunMetadata.start("ecosystem-audit")
    units: list = []
    roots: dict = {}
    repo_files: dict = {}
    repos = [SimpleNamespace(name="done"), SimpleNamespace(name="todo")]
    eco._scan_and_extract(repos, meta, {"done"}, units, roots, repo_files)
    # Before: "done" was skipped and so dropped from the graph, embeddings and reports.
    assert units == ["done:a", "todo:a"]
    assert set(repo_files) == {"done", "todo"}
    assert meta.scanned_repos == ["done", "todo"] and meta.scanned_files == 2


def test_cleanup_failure_neither_masks_the_error_nor_leaks_the_graph(tmp_path, monkeypatch) -> None:
    events: list[str] = []

    class Graph:
        def __init__(self, path):
            pass

        def close(self):
            events.append("graph.close")

    def finish(meta, pipeline):
        raise ConnectionError("db down")

    cfg = SimpleNamespace(log_level="INFO", log_file_path="", output_dir=str(tmp_path), repos=[],
                          graph_db_path=str(tmp_path / "g.db"), auth_mode=False, auth_code_env="")
    monkeypatch.setattr(eco, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(eco, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(eco, "load_architecture_rules", dict)
    monkeypatch.setattr(eco, "GraphBuilder", Graph)
    monkeypatch.setattr(eco, "record_run_progress", lambda meta: None)
    monkeypatch.setattr(eco, "get_settings", lambda: SimpleNamespace())

    def broken_auth(*a, **k):
        raise PermissionError("bad auth code")

    monkeypatch.setattr(eco, "require_auth", broken_auth)
    monkeypatch.setattr(eco, "record_run_finish", finish)
    monkeypatch.setattr(eco, "write_run_metadata", lambda meta, output_dir: events.append("metadata"))
    with pytest.raises(PermissionError, match="bad auth code"):  # was ConnectionError
        eco.run()
    assert events == ["graph.close", "metadata"]
