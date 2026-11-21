"""A progress tick racing the finish never overwrites the final metadata (pack 987780c2b9)."""
from __future__ import annotations

import uuid
from dataclasses import asdict as real_asdict

from sqlalchemy import select

from auditor import persistence
from auditor.db import AuditRun, db_session
from auditor.runtime import RunMetadata


def test_finish_landing_between_the_read_and_the_write_wins(monkeypatch):
    rid = f"t-{uuid.uuid4().hex}"
    persistence.record_run_start(rid, "ecosystem-audit")
    tick = RunMetadata.start("t", rid)
    tick.current_stage = "late tick"
    done = RunMetadata.start("t", rid)
    done.finish("success")
    fired = []

    def asdict_after_finish(obj):
        # The tick has read the row (finished_at still NULL); the finisher commits now.
        if obj is tick and not fired:
            fired.append(1)
            persistence.record_run_finish(done, "ecosystem-audit")
        return real_asdict(obj)

    monkeypatch.setattr(persistence, "asdict", asdict_after_finish)
    persistence.record_run_progress(tick)
    assert fired
    with db_session() as session:
        row = session.execute(select(AuditRun).where(AuditRun.run_id == rid)).scalar_one()
        assert row.status == "success"
        assert row.metadata_json.get("current_stage") != "late tick"
        assert row.metadata_json["status"] == "success"
