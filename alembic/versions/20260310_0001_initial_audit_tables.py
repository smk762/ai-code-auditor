"""initial audit tables

Revision ID: 20260310_0001
Revises: None
Create Date: 2026-03-10 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260310_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("pipeline", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_audit_runs_run_id", "audit_runs", ["run_id"], unique=True)
    op.create_index("ix_audit_runs_pipeline", "audit_runs", ["pipeline"])
    op.create_index("ix_audit_runs_status", "audit_runs", ["status"])

    op.create_table(
        "repo_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=128), sa.ForeignKey("audit_runs.run_id"), nullable=False),
        sa.Column("repo_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_repo_runs_run_id", "repo_runs", ["run_id"])
    op.create_index("ix_repo_runs_repo_name", "repo_runs", ["repo_name"])
    op.create_index("ix_repo_runs_status", "repo_runs", ["status"])

    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finding_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_findings_run_id", "findings", ["run_id"])
    op.create_index("ix_findings_repo", "findings", ["repo"])
    op.create_index("ix_findings_severity", "findings", ["severity"])

    op.create_table(
        "auth_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("details", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_audit_events_actor", "auth_audit_events", ["actor"])
    op.create_index("ix_auth_audit_events_action", "auth_audit_events", ["action"])
    op.create_index("ix_auth_audit_events_status", "auth_audit_events", ["status"])

    op.create_table(
        "pipeline_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("repo_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pipeline_checkpoints_pipeline", "pipeline_checkpoints", ["pipeline"])
    op.create_index("ix_pipeline_checkpoints_run_id", "pipeline_checkpoints", ["run_id"])
    op.create_index("ix_pipeline_checkpoints_repo_name", "pipeline_checkpoints", ["repo_name"])
    op.create_index("ix_pipeline_checkpoints_status", "pipeline_checkpoints", ["status"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_checkpoints_status", table_name="pipeline_checkpoints")
    op.drop_index("ix_pipeline_checkpoints_repo_name", table_name="pipeline_checkpoints")
    op.drop_index("ix_pipeline_checkpoints_run_id", table_name="pipeline_checkpoints")
    op.drop_index("ix_pipeline_checkpoints_pipeline", table_name="pipeline_checkpoints")
    op.drop_table("pipeline_checkpoints")

    op.drop_index("ix_auth_audit_events_status", table_name="auth_audit_events")
    op.drop_index("ix_auth_audit_events_action", table_name="auth_audit_events")
    op.drop_index("ix_auth_audit_events_actor", table_name="auth_audit_events")
    op.drop_table("auth_audit_events")

    op.drop_index("ix_findings_severity", table_name="findings")
    op.drop_index("ix_findings_repo", table_name="findings")
    op.drop_index("ix_findings_run_id", table_name="findings")
    op.drop_table("findings")

    op.drop_index("ix_repo_runs_status", table_name="repo_runs")
    op.drop_index("ix_repo_runs_repo_name", table_name="repo_runs")
    op.drop_index("ix_repo_runs_run_id", table_name="repo_runs")
    op.drop_table("repo_runs")

    op.drop_index("ix_audit_runs_status", table_name="audit_runs")
    op.drop_index("ix_audit_runs_pipeline", table_name="audit_runs")
    op.drop_index("ix_audit_runs_run_id", table_name="audit_runs")
    op.drop_table("audit_runs")
