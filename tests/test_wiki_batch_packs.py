"""Regression tests for wiki/batch.py audit packs."""
from pathlib import Path

import pytest

from wiki.batch import Result, Runner


def test_store_crash_mid_write_keeps_the_previous_result(tmp_path: Path, monkeypatch) -> None:
    runner = Runner(tmp_path, client=object())
    runner._store(Result("n-1", text="first reply", cost=0.5))
    real_write = Path.write_text

    def crash_halfway(self, data, *a, **k):
        real_write(self, data[: len(data) // 2], *a, **k)
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", crash_halfway)
    with pytest.raises(OSError):
        runner._store(Result("n-1", text="second reply " * 50, cost=0.7))
    monkeypatch.setattr(Path, "write_text", real_write)
    # the paid-for result is still readable, so it isn't requested (and charged) again
    assert runner._stored("n-1") == Result("n-1", text="first reply", cost=0.5)


def test_store_crash_on_a_new_result_leaves_no_truncated_file(tmp_path: Path, monkeypatch) -> None:
    runner = Runner(tmp_path, client=object())
    real_write = Path.write_text

    def crash_halfway(self, data, *a, **k):
        real_write(self, data[: len(data) // 2], *a, **k)
        raise OSError("killed")

    monkeypatch.setattr(Path, "write_text", crash_halfway)
    with pytest.raises(OSError):
        runner._store(Result("n-2", text="reply"))
    monkeypatch.setattr(Path, "write_text", real_write)
    assert not runner._result_path("n-2").exists()


def test_store_round_trips(tmp_path: Path) -> None:
    runner = Runner(tmp_path, client=object())
    runner._store(Result("n-3", text="ok", stop_reason="end_turn", cost=0.1))
    assert runner._stored("n-3") == Result("n-3", text="ok", stop_reason="end_turn", cost=0.1)
    assert [p.name for p in runner.results_dir.iterdir()] == ["n-3.json"]
