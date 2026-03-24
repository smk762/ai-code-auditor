from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import delete, select

from auditor.contracts import Finding
from auditor.db import AuditRun, AuthAuditEvent, FindingRecord, PipelineCheckpoint, RepoRun, db_session, utcnow
from auditor.runtime import RunMetadata


def record_run_start(run_id: str, pipeline: str) -> None:
    with db_session() as session:
        row = session.execute(select(AuditRun).where(AuditRun.run_id == run_id)).scalar_one_or_none()
        if row is None:
            session.add(
                AuditRun(
                    run_id=run_id,
                    pipeline=pipeline,
                    started_at=utcnow(),
                    finished_at=None,
                    status="running",
                    metadata_json={},
                )
            )
        else:
            row.status = "running"
            row.started_at = utcnow()


def record_run_progress(metadata: RunMetadata) -> None:
    """Merge live progress into ``audit_runs.metadata_json`` without finishing the run."""
    with db_session() as session:
        row = session.execute(select(AuditRun).where(AuditRun.run_id == metadata.run_id)).scalar_one_or_none()
        if row is None:
            return
        row.metadata_json = asdict(metadata)


def record_run_finish(metadata: RunMetadata, pipeline: str) -> None:
    with db_session() as session:
        row = session.execute(select(AuditRun).where(AuditRun.run_id == metadata.run_id)).scalar_one_or_none()
        if row is None:
            row = AuditRun(
                run_id=metadata.run_id,
                pipeline=pipeline,
                started_at=utcnow(),
                status=metadata.status,
                metadata_json=asdict(metadata),
            )
            session.add(row)
        row.finished_at = utcnow()
        row.status = metadata.status
        payload = asdict(metadata)
        if row.started_at and row.finished_at:
            payload["duration_ms"] = int((row.finished_at - row.started_at).total_seconds() * 1000)
        row.metadata_json = payload


def upsert_repo_run(run_id: str, repo_name: str, status: str, attempts: int = 1, error_message: str = "") -> None:
    with db_session() as session:
        row = session.execute(
            select(RepoRun).where(RepoRun.run_id == run_id, RepoRun.repo_name == repo_name)
        ).scalar_one_or_none()
        if row is None:
            row = RepoRun(
                run_id=run_id,
                repo_name=repo_name,
                status=status,
                attempts=attempts,
                error_message=error_message,
                updated_at=utcnow(),
            )
            session.add(row)
            return
        row.status = status
        row.attempts = attempts
        row.error_message = error_message
        row.updated_at = utcnow()


def store_findings(run_id: str, findings: list[Finding]) -> None:
    with db_session() as session:
        session.execute(delete(FindingRecord).where(FindingRecord.run_id == run_id))
        for finding in findings:
            session.add(
                FindingRecord(
                    run_id=run_id,
                    repo=finding.repo,
                    file_path=finding.file_path,
                    line=finding.line,
                    finding_type=finding.type,
                    severity=finding.severity,
                    title=finding.title,
                    source=finding.source,
                    created_at=utcnow(),
                )
            )


def write_checkpoint(pipeline: str, run_id: str, repo_name: str, status: str, payload: dict | None = None) -> None:
    payload = payload or {}
    with db_session() as session:
        row = session.execute(
            select(PipelineCheckpoint).where(
                PipelineCheckpoint.pipeline == pipeline,
                PipelineCheckpoint.run_id == run_id,
                PipelineCheckpoint.repo_name == repo_name,
            )
        ).scalar_one_or_none()
        if row is None:
            row = PipelineCheckpoint(
                pipeline=pipeline,
                run_id=run_id,
                repo_name=repo_name,
                status=status,
                payload=payload,
                updated_at=utcnow(),
            )
            session.add(row)
            return
        row.status = status
        row.payload = payload
        row.updated_at = utcnow()


def completed_repos_for_run(pipeline: str, run_id: str) -> set[str]:
    with db_session() as session:
        rows = session.execute(
            select(PipelineCheckpoint.repo_name).where(
                PipelineCheckpoint.pipeline == pipeline,
                PipelineCheckpoint.run_id == run_id,
                PipelineCheckpoint.status == "completed",
            )
        ).all()
        return {row[0] for row in rows}


def audit_auth_event(actor: str, action: str, status: str, details: str = "") -> None:
    with db_session() as session:
        session.add(
            AuthAuditEvent(
                actor=actor,
                action=action,
                status=status,
                details=details,
                created_at=utcnow(),
            )
        )


def recent_findings(limit: int = 1000) -> list[dict]:
    with db_session() as session:
        rows = session.execute(select(FindingRecord).order_by(FindingRecord.id.desc()).limit(limit)).scalars().all()
        return [
            {
                "run_id": row.run_id,
                "repo": row.repo,
                "file_path": row.file_path,
                "line": row.line,
                "severity": row.severity,
                "finding_type": row.finding_type,
                "source": row.source,
            }
            for row in rows
        ]
