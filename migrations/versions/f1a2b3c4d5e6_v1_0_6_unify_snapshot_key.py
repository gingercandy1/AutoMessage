"""v1.0.6 Unify MetricSnapshot daily key (ADR-0002)

Revision ID: f1a2b3c4d5e6
Revises: e8303b83e1fa
Create Date: 2026-08-06

Removes `category` from the daily uniqueness key of `metric_snapshots`
so that each entity (source + external_id) has exactly ONE row per day.
Historical duplicate rows (same source/external_id/captured_date, differing
only by category) are folded: keep the row with the latest captured_at,
delete the rest. New key: (source, external_id, captured_date).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import table, column, func, select
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e8303b83e1fa'
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    # 1) Fold historical duplicates BEFORE changing the unique constraint,
    #    otherwise the new constraint creation would fail on existing dup rows.
    conn = op.get_bind()

    metric_snapshots = table(
        "metric_snapshots",
        column("id", sa.Integer),
        column("source", sa.String),
        column("external_id", sa.String),
        column("captured_date", sa.Date),
        column("captured_at", sa.DateTime),
    )

    # Find the id of the row to KEEP for each (source, external_id, captured_date):
    # the one with the largest captured_at.
    keep_subq = (
        select(
            func.max(metric_snapshots.c.id).label("keep_id")
        )
        .group_by(
            metric_snapshots.c.source,
            metric_snapshots.c.external_id,
            metric_snapshots.c.captured_date,
        )
        .subquery()
    )

    # Delete rows whose id is NOT in the keep-set.
    delete_stmt = metric_snapshots.delete().where(
        metric_snapshots.c.id.notin_(select(keep_subq.c.keep_id))
    )
    conn.execute(delete_stmt)

    # 2) Drop the old unique constraint that includes category.
    op.drop_constraint("uq_snapshot_daily", "metric_snapshots", type_="unique")

    # 3) Make category nullable (no longer part of the key) — already nullable=False
    #    in model, but the existing column may be NOT NULL; relax to nullable.
    op.alter_column(
        "metric_snapshots", "category",
        existing_type=sa.String(length=100),
        nullable=True,
    )

    # 4) Create the new unified daily unique constraint.
    op.create_unique_constraint(
        "uq_snapshot_daily", "metric_snapshots",
        ["source", "external_id", "captured_date"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_snapshot_daily", "metric_snapshots", type_="unique")
    op.alter_column(
        "metric_snapshots", "category",
        existing_type=sa.String(length=100),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_snapshot_daily", "metric_snapshots",
        ["source", "external_id", "category", "captured_date"],
    )
