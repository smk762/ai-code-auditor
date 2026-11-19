from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pipelines.ecosystem_audit as eco


def test_setup_failure_after_acquiring_capacity_still_releases_it(tmp_path, monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []

    class Scheduler:
        def wait_for_capacity(self, on_wait_update=None):
            events.append("acquire")
            return SimpleNamespace(offload_mode="cpu", num_gpu_layers=0, vram_free_mb=0)

        def release_capacity(self):
            events.append("release")

    cfg = SimpleNamespace(log_level="INFO", log_file_path="", output_dir=str(tmp_path), repos=[],
                          graph_db_path=str(tmp_path / "g.db"))
    monkeypatch.setenv("AI_AUDIT_SKIP_GPU_SCHEDULER", "1")
    monkeypatch.setattr(eco, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(eco, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(eco, "load_architecture_rules", dict)
    monkeypatch.setattr(eco, "GpuScheduler", Scheduler)
    monkeypatch.setattr(eco, "record_run_finish", lambda meta, pipeline: events.append(f"finish:{meta.status}"))
    monkeypatch.setattr(eco, "write_run_metadata", lambda meta, output_dir: tmp_path / "run.json")

    def broken_graph(path):
        raise OSError("graph DB unavailable")

    monkeypatch.setattr(eco, "GraphBuilder", broken_graph)
    monkeypatch.setattr(eco, "record_run_progress", lambda meta: None)
    with pytest.raises(OSError):
        eco.run()
    assert events == ["acquire", "release", "finish:failed"]


def test_repo_failing_mid_extraction_contributes_no_units(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from auditor.runtime import RunMetadata

    good, bad = tmp_path / "good", tmp_path / "bad"
    for d in (good, bad):
        (d / ".git").mkdir(parents=True)
    files = {"good": [str(good / "a.py"), str(good / "b.py")], "bad": [str(bad / "a.py"), str(bad / "b.py")]}
    statuses: list[tuple[str, str]] = []

    def extract(repo_name, file_path):
        if repo_name == "bad" and file_path.endswith("b.py"):
            raise ValueError("parse error")
        return [f"{repo_name}:{Path(file_path).name}"]

    monkeypatch.setattr(eco, "scan_repo", lambda repo: files[repo.name])
    monkeypatch.setattr(eco, "extract_code_units", extract)
    monkeypatch.setattr(eco, "upsert_repo_run", lambda run_id, name, status, **kw: statuses.append((name, status)))
    monkeypatch.setattr(eco, "write_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(eco, "record_run_progress", lambda meta: None)

    meta = RunMetadata.start("ecosystem-audit")
    units: list = []
    roots: dict = {}
    repo_files: dict = {}
    repos = [SimpleNamespace(name="bad"), SimpleNamespace(name="good")]
    eco._scan_and_extract(repos, meta, set(), units, roots, repo_files)
    assert statuses == [("bad", "failed"), ("good", "completed")]
    assert units == ["good:a.py", "good:b.py"]
    assert set(roots) == set(repo_files) == {"good"}
    assert meta.code_units == 2 and meta.scanned_repos == ["good"]
