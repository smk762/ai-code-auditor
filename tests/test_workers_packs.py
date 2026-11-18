"""run_worker records setup failures as FAILED results instead of raising."""

from __future__ import annotations

from conductor import workers
from conductor.missions import WorkerTask


def _task(mission: str = "bughunt") -> WorkerTask:
    return WorkerTask(id="t-1", mission=mission, title="t", repo="r", repo_path="/x", prompt="p")  # type: ignore[arg-type]


def test_bad_model_config_is_a_failed_result(monkeypatch):
    def boom():
        raise ValueError("bad conductor config")

    monkeypatch.setattr(workers, "load_conductor_models", boom)
    result = workers.run_worker(_task())
    assert result.status == "FAILED"
    assert "bad conductor config" in (result.escalate_reason or "")


def test_unknown_mission_is_a_failed_result(monkeypatch):
    monkeypatch.setattr(workers, "completion", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    result = workers.run_worker(_task("no-such-mission"))
    assert result.status == "FAILED"
    assert result.task_id == "t-1"
    assert result.escalate_reason
