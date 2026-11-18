from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auditor import persistence
from auditor.db import AuditRun, PipelineCheckpoint, RepoRun, db_session, utcnow
from auditor.runtime import RunMetadata

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261118_0002_unique_run_state_keys.py"


def _run(run_id: str) -> AuditRun:
    with db_session() as session:
        row = session.execute(select(AuditRun).where(AuditRun.run_id == run_id)).scalar_one()
        session.expunge(row)
        return row


def test_restart_clears_finish_and_takes_the_new_pipeline():
    rid = f"t-{uuid.uuid4().hex}"
    persistence.record_run_start(rid, "repo-audit")
    meta = RunMetadata.start("t", rid)
    meta.finish("success")
    persistence.record_run_finish(meta, "repo-audit")
    assert _run(rid).finished_at is not None
    persistence.record_run_start(rid, "ecosystem-audit")
    row = _run(rid)
    assert row.status == "running" and row.finished_at is None and row.pipeline == "ecosystem-audit"


def test_progress_merges_and_never_touches_a_finished_run():
    rid = f"t-{uuid.uuid4().hex}"
    persistence.record_run_start(rid, "ecosystem-audit")
    with db_session() as session:
        row = session.execute(select(AuditRun).where(AuditRun.run_id == rid)).scalar_one()
        row.metadata_json = {"note": "kept"}
    meta = RunMetadata.start("t", rid)
    meta.current_stage = "scan"
    persistence.record_run_progress(meta)
    got = _run(rid).metadata_json
    assert got["note"] == "kept" and got["current_stage"] == "scan"

    done = RunMetadata.start("t", rid)
    done.finish("success")
    persistence.record_run_finish(done, "ecosystem-audit")
    final = _run(rid).metadata_json
    meta.current_stage = "late tick"
    persistence.record_run_progress(meta)
    row = _run(rid)
    assert row.status == "success" and row.metadata_json == final


def test_one_state_row_per_repo_and_checkpoint_key():
    rid = f"t-{uuid.uuid4().hex}"
    persistence.record_run_start(rid, "repo-audit")
    with pytest.raises(IntegrityError), db_session() as session:
        for _ in range(2):
            session.add(RepoRun(run_id=rid, repo_name="r", status="completed", attempts=1,
                                error_message="", updated_at=utcnow()))
    with pytest.raises(IntegrityError), db_session() as session:
        for _ in range(2):
            session.add(PipelineCheckpoint(pipeline="p", run_id=rid, repo_name="r", status="completed",
                                           payload={}, updated_at=utcnow()))


def test_migration_collapses_duplicates_then_adds_unique_keys(tmp_path: Path):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    spec = importlib.util.spec_from_file_location("mig_0002", MIGRATION)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "20260310_0001"

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    repo_runs = sa.Table("repo_runs", md, sa.Column("id", sa.Integer, primary_key=True),
                         sa.Column("run_id", sa.String(128)), sa.Column("repo_name", sa.String(255)),
                         sa.Column("status", sa.String(32)))
    cps = sa.Table("pipeline_checkpoints", md, sa.Column("id", sa.Integer, primary_key=True),
                   sa.Column("pipeline", sa.String(64)), sa.Column("run_id", sa.String(128)),
                   sa.Column("repo_name", sa.String(255)), sa.Column("status", sa.String(32)))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(repo_runs.insert(), [
            {"run_id": "a", "repo_name": "r", "status": "old"},
            {"run_id": "a", "repo_name": "r", "status": "new"},
            {"run_id": "a", "repo_name": "s", "status": "only"},
        ])
        conn.execute(cps.insert(), [
            {"pipeline": "p", "run_id": "a", "repo_name": "r", "status": "old"},
            {"pipeline": "p", "run_id": "a", "repo_name": "r", "status": "new"},
        ])
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
    with engine.begin() as conn:
        assert sorted(r.status for r in conn.execute(sa.text("SELECT status FROM repo_runs"))) == ["new", "only"]
        assert [r.status for r in conn.execute(sa.text("SELECT status FROM pipeline_checkpoints"))] == ["new"]
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO repo_runs (run_id, repo_name, status) VALUES ('a', 'r', 'dup')"))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO pipeline_checkpoints (pipeline, run_id, repo_name, status) VALUES ('p', 'a', 'r', 'dup')"))
