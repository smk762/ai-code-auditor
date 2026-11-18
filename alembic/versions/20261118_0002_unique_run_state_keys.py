"""unique per-repo run state keys

One ``repo_runs`` row per (run_id, repo_name) and one ``pipeline_checkpoints`` row per
(pipeline, run_id, repo_name): the writers update that row, and resume logic reads it. Existing
duplicates are collapsed to the newest row (highest id) before the constraints are added.

Revision ID: 20261118_0002
Revises: 20260310_0001
Create Date: 2026-11-18 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20261118_0002"
down_revision = "20260310_0001"
branch_labels = None
depends_on = None

_KEYS = {
    "repo_runs": ("uq_repo_runs_run_id_repo_name", ["run_id", "repo_name"]),
    "pipeline_checkpoints": (
        "uq_pipeline_checkpoints_pipeline_run_id_repo_name",
        ["pipeline", "run_id", "repo_name"],
    ),
}


def upgrade() -> None:
    for table, (name, cols) in _KEYS.items():
        group = ", ".join(cols)
        # The derived table keeps MySQL-style engines happy and is portable to Postgres/SQLite.
        op.execute(sa.text(
            f"DELETE FROM {table} WHERE id NOT IN "
            f"(SELECT keep FROM (SELECT MAX(id) AS keep FROM {table} GROUP BY {group}) AS newest)"
        ))
        with op.batch_alter_table(table) as batch:
            batch.create_unique_constraint(name, cols)


def downgrade() -> None:
    for table, (name, _cols) in _KEYS.items():
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="unique")
