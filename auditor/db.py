from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from auditor.settings import get_settings, normalize_database_url


class Base(DeclarativeBase):
    pass


class AuditRun(Base):
    __tablename__ = "audit_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    pipeline: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    repo_runs: Mapped[list["RepoRun"]] = relationship(back_populates="audit_run")


class RepoRun(Base):
    __tablename__ = "repo_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.run_id"), index=True)
    repo_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error_message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    audit_run: Mapped["AuditRun"] = relationship(back_populates="repo_runs")


class FindingRecord(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    file_path: Mapped[str] = mapped_column(Text)
    line: Mapped[int] = mapped_column(Integer, default=0)
    finding_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthAuditEvent(Base):
    __tablename__ = "auth_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PipelineCheckpoint(Base):
    __tablename__ = "pipeline_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    repo_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


settings = get_settings()
normalized_database_url = normalize_database_url(settings.database_url)
if normalized_database_url.startswith("sqlite"):
    engine = create_engine(normalized_database_url, future=True)
else:
    engine = create_engine(
        normalized_database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        future=True,
    )
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
if normalized_database_url.startswith("sqlite"):
    Base.metadata.create_all(bind=engine)


@contextmanager
def db_session() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
