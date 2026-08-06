"""v1_0_8 create news_clusters table

Revision ID: a7b8c9d0e1f3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-06

Q10: 新闻去重聚类表 NewsCluster（模型已定义，此前缺迁移）。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("news_clusters"):
        op.create_table(
            "news_clusters",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("canonical_item_id", sa.Integer(), sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("member_item_ids", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
            sa.Column("cluster_method", sa.String(50), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("idx_news_cluster_canonical", "news_clusters", ["canonical_item_id"])
    else:
        # 表已存在（可能历史 create_all 创建），确保索引存在
        existing_indexes = [i["name"] for i in inspector.get_indexes("news_clusters")]
        if "idx_news_cluster_canonical" not in existing_indexes:
            op.create_index("idx_news_cluster_canonical", "news_clusters", ["canonical_item_id"])


def downgrade():
    op.drop_index("idx_news_cluster_canonical", table_name="news_clusters")
    op.drop_table("news_clusters")
