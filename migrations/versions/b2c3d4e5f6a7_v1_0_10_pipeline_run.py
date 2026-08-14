"""v1.0.10 add PipelineRun table (Q7 observability, #03 close).

Revision ID: b2c3d4e5f6a7
Revises: a7b8c9d0e1f4
Create Date: 2026-08-06

Records per-(run_type, source) pipeline run metrics so /health can surface the
last collection/processing outcome without re-aggregating ContentItem counts.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a7b8c9d0e1f4"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_type", sa.String(length=30), nullable=False, server_default="daily"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("collected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_by_stage", sa.JSON(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
    )
    op.create_index("idx_pipeline_run_started", "pipeline_runs", ["started_at"])
    op.create_index(
        "idx_pipeline_run_type_source", "pipeline_runs", ["run_type", "source"]
    )


def downgrade() -> None:
    op.drop_index("idx_pipeline_run_type_source", table_name="pipeline_runs")
    op.drop_index("idx_pipeline_run_started", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
