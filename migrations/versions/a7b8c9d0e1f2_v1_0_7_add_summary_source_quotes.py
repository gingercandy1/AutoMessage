"""v1_0_7 add Summary.source_quotes

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-06

Q6: 新闻独立 schema —— Summary 加 source_quotes（原文摘录列表，保障 Q3 准确性）。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "summaries",
        sa.Column("source_quotes", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )


def downgrade():
    op.drop_column("summaries", "source_quotes")
