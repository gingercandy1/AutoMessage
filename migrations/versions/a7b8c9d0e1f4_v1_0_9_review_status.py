"""v1_0_9 add ContentItem.review_status

Revision ID: a7b8c9d0e1f4
Revises: a7b8c9d0e1f3
Create Date: 2026-08-06

Q4: 人工审查闸门所需的审查状态列。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f4"
down_revision = "a7b8c9d0e1f3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "content_items",
        sa.Column(
            "review_status", sa.String(20), nullable=True, default=None,
            comment="审查状态: NULL=未送审, pending=审查中, approved=通过, rejected=拒绝",
        ),
    )
    op.create_index("idx_review_status", "content_items", ["review_status"])


def downgrade():
    op.drop_index("idx_review_status", table_name="content_items")
    op.drop_column("content_items", "review_status")
