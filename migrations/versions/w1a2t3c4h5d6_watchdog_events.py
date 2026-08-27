"""add watchdog_events (watchdog persistence)

Revision ID: w1a2t3c4h5d6
Revises: db20ef5d7464
Create Date: 2026-08-27

Watchdog traceability: persist alerts so /watchdog and Bot /watchdog are auditable.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "w1a2t3c4h5d6"
down_revision: Union[str, None] = "db20ef5d7464"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None

def upgrade() -> None:
    op.create_table(
        "watchdog_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("check_name", sa.String(length=80), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False, server_default="warning"),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("suggestion", sa.String(length=500), nullable=True),
        sa.Column("resolved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_watchdog_check_created", "watchdog_events", ["check_name", "created_at"])
    op.create_index("idx_watchdog_level_created", "watchdog_events", ["level", "created_at"])

def downgrade() -> None:
    op.drop_index("idx_watchdog_level_created", table_name="watchdog_events")
    op.drop_index("idx_watchdog_check_created", table_name="watchdog_events")
    op.drop_table("watchdog_events")
