"""An interrupted ecosystem run is recorded with a terminal status (batch-5 pack)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import pipelines.ecosystem_audit as eco


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupted_run_is_recorded_as_cancelled(tmp_path, monkeypatch, interrupt) -> None:
    recorded: list = []

    class Graph:
        def __init__(self, path):
            pass

        def close(self):
            pass

    cfg = SimpleNamespace(log_level="INFO", log_file_path="", output_dir=str(tmp_path), repos=[],
                          graph_db_path=str(tmp_path / "g.db"), auth_mode=False, auth_code_env="")
    monkeypatch.setattr(eco, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setattr(eco, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(eco, "load_architecture_rules", dict)
    monkeypatch.setattr(eco, "GraphBuilder", Graph)
    monkeypatch.setattr(eco, "record_run_progress", lambda meta: None)
    monkeypatch.setattr(eco, "get_settings", lambda: SimpleNamespace())

    def interrupted(*a, **k):
        raise interrupt()

    monkeypatch.setattr(eco, "require_auth", interrupted)
    monkeypatch.setattr(eco, "record_run_finish",
                        lambda meta, pipeline: recorded.append((meta.status, meta.finished_at, list(meta.errors))))
    monkeypatch.setattr(eco, "write_run_metadata", lambda meta, output_dir: None)
    with pytest.raises(interrupt):
        eco.run()
    [(status, finished_at, errors)] = recorded
    assert status == "cancelled"  # was "running": the run looked like it never ended
    assert finished_at
    assert errors and interrupt.__name__ in errors[0]
