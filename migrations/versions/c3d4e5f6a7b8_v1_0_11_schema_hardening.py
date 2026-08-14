"""v1.0.11 schema hardening (model stability / extensibility pass).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-06

Brings the live schema in line with models.py after the stability review:
  - content_items.metrics: JSON -> JSONB (enables GIN index / filtering later)
  - news_clusters.member_item_ids: JSON -> JSONB (consistent with MetricSnapshot)
  - content_items.url: NOT NULL -> nullable (multi-source URL may be missing)
  - users.api_quota / api_used: dropped (dead fields, never consumed)
  - users.created_at/last_active, subscriptions.created_at,
    user_favorites.created_at: timestamp -> timestamptz (consistent tz)
  - subscriptions.subscription_type: CHECK constraint (language/tag/author/source)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    # S3: JSON -> JSONB (preserves data via USING cast)
    op.execute("ALTER TABLE content_items ALTER COLUMN metrics TYPE jsonb USING metrics::jsonb")
    op.execute("ALTER TABLE news_clusters ALTER COLUMN member_item_ids TYPE jsonb USING member_item_ids::jsonb")

    # S4: url nullable
    op.alter_column("content_items", "url", existing_type=sa.String(), nullable=True)

    # S1: drop dead quota columns
    op.drop_column("users", "api_quota")
    op.drop_column("users", "api_used")

    # S5: timestamp -> timestamptz
    op.alter_column("users", "created_at",
                    existing_type=sa.DateTime(), type_=sa.DateTime(timezone=True),
                    existing_nullable=False)
    op.alter_column("users", "last_active",
                    existing_type=sa.DateTime(), type_=sa.DateTime(timezone=True),
                    existing_nullable=False)
    op.alter_column("subscriptions", "created_at",
                    existing_type=sa.DateTime(), type_=sa.DateTime(timezone=True),
                    existing_nullable=False)
    op.alter_column("user_favorites", "created_at",
                    existing_type=sa.DateTime(), type_=sa.DateTime(timezone=True),
                    existing_nullable=False)

    # S2: dimension whitelist
    op.create_check_constraint(
        "ck_subscription_type",
        "subscriptions",
        sa.Column("subscription_type", sa.String(length=100), nullable=False).in_(
            ["language", "tag", "author", "source"]
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_subscription_type", "subscriptions", type_="check")

    op.alter_column("user_favorites", "created_at",
                    existing_type=sa.DateTime(timezone=True), type_=sa.DateTime(),
                    existing_nullable=False)
    op.alter_column("subscriptions", "created_at",
                    existing_type=sa.DateTime(timezone=True), type_=sa.DateTime(),
                    existing_nullable=False)
    op.alter_column("users", "last_active",
                    existing_type=sa.DateTime(timezone=True), type_=sa.DateTime(),
                    existing_nullable=False)
    op.alter_column("users", "created_at",
                    existing_type=sa.DateTime(timezone=True), type_=sa.DateTime(),
                    existing_nullable=False)

    op.add_column("users", sa.Column("api_used", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("api_quota", sa.Integer(), nullable=False, server_default="100"))

    op.alter_column("content_items", "url", existing_type=sa.String(), nullable=False)

    op.execute("ALTER TABLE news_clusters ALTER COLUMN member_item_ids TYPE json USING member_item_ids::json")
    op.execute("ALTER TABLE content_items ALTER COLUMN metrics TYPE json USING metrics::json")
