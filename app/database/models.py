import json
from datetime import datetime, UTC, timezone
from typing import Any

from sqlalchemy import (Column, Integer, String, Text, DateTime, Float,
                        ForeignKey, UniqueConstraint, JSON, Index, BigInteger, Date)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class ContentItem(Base):
    __tablename__ = "content_items"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String)  # GitHub, x, hacker_news, news
    external_id = Column(String(255), nullable=False)  # 代替repository，各平台原始ID,配合source联合唯一
    title = Column(String, index=True)
    url = Column(String, unique=True)
    description = Column(String)
    author = Column(String)
    category = Column(String(50), nullable=True, index=True)  # 新闻分类: ai, llm, research, tools, industry

    summary = relationship('Summary', back_populates='project', uselist=False)
    favorites = relationship("UserFavorite", back_populates="project", cascade="all, delete-orphan")

    discovered_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    processed = Column(Integer, default=0)  # 0=未处理, 1=已处理

    metrics = Column(JSON)  # {"stars": 100, "downloads": 5000, "likes": 20, ...} 灵活存各平台指标
    raw_data = Column(JSON)  # 保留原始API返回,方便以后回溯/重新处理

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_source_external_id"),
        Index("idx_source_discovered", "source", "discovered_at"),
        Index("idx_source_category", "source", "category"),
    )


class User(Base):
    """用户表"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, unique=True, comment="Telegram ID")

    username = Column(String(255), nullable=True, comment="Telegram 用户名")
    first_name = Column(String(255), nullable=True, comment="名字")
    last_name = Column(String(255), nullable=True, comment="姓氏")

    subscription_tier = Column(String(50), nullable=False, default="free", comment="订阅等级（free/pro/premium）")
    api_quota = Column(Integer, nullable=False, default=100, comment="API 调用配额")
    api_used = Column(Integer, nullable=False, default=0, comment="已使用 API 调用")

    preferred_languages = Column(JSON, nullable=True, comment="偏好语言列表")
    preferred_tags = Column(JSON, nullable=True, comment="偏好标签列表")

    created_at = Column(DateTime, nullable=False, default=datetime.now(UTC), comment="注册时间")
    last_active = Column(DateTime, nullable=False, default=datetime.now(UTC), comment="最后活跃时间")

    __table_args__ = (
        Index("idx_telegram_id", "telegram_id"),
        Index("idx_subscription_tier", "subscription_tier"),
    )

    def __repr__(self):
        return f"<User(telegram_id={self.telegram_id}, tier={self.subscription_tier})>"


class UserFavorite(Base):
    """用户收藏表"""

    __tablename__ = "user_favorites"

    id = Column(Integer, primary_key=True, autoincrement=True)

    telegram_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    project_id = Column(Integer, ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False)

    created_at = Column(DateTime, nullable=False, default=datetime.now(UTC), comment="收藏时间")
    project = relationship("ContentItem", back_populates="favorites")

    __table_args__ = (
        Index("idx_telegram_id_project_id", "telegram_id", "project_id"),
        Index('idx_user_favorites_project_id', 'project_id'),
    )

    def __repr__(self):
        return f"<UserFavorite(telegram_id={self.telegram_id}, project_id={self.project_id})>"


class Subscription(Base):
    """订阅表"""

    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, comment="Telegram ID")

    subscription_type = Column(String(100), nullable=False, comment="订阅类型（language/tag/author）")
    subscription_value = Column(String(255), nullable=False, comment="订阅值（python/AI/xxx）")

    is_active = Column(Integer, nullable=False, default=1, comment="是否活跃")
    created_at = Column(DateTime, nullable=False, default=datetime.now(UTC), comment="订阅时间")

    __table_args__ = (
        Index("idx_telegram_subscription", "telegram_id", "subscription_type", "subscription_value"),
    )

    def __repr__(self):
        return f"<Subscription(telegram_id={self.telegram_id}, type={self.subscription_type}, value={self.subscription_value})>"


class Summary(Base):
    """项目摘要表"""
    __tablename__ = "summaries"

    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, unique=True)

    # 内容字段
    one_liner = Column(String(255), nullable=False, comment="一句话介绍")
    summary = Column(Text, nullable=False, comment="详细介绍（200-500字）")
    use_cases = Column(Text, nullable=True, comment="使用场景")

    # JSON 数组字段
    pros = Column(JSON, nullable=True, comment="优点列表（JSON 数组）")  # ["优点1", "优点2", ...]
    cons = Column(JSON, nullable=True, comment="缺点列表（JSON 数组）")  # ["缺点1", "缺点2", ...]
    tags = Column(JSON, nullable=True, comment="标签列表（JSON 数组）")  # ["tag1", "tag2", ...]

    # 评分和步骤
    score = Column(Float, nullable=False, default=0.0, comment="综合评分（0-10）")
    install_step = Column(Text, nullable=True, comment="安装/使用步骤")
    maturity_level = Column(Text, nullable=True, comment="成熟度评估")

    # 新闻专用：原文摘录（保障准确性，Q3）。Q6 独立 schema 字段之一。
    source_quotes = Column(JSON, nullable=True, comment="新闻原文摘录列表（JSON 数组，2-3句）")

    # 元数据
    model_used = Column(String(100), nullable=False, comment="使用的 AI 模型（ollama/openrouter/openai）")
    processing_status = Column(String(50), nullable=False, default="success", comment="处理状态（success/failed/pending）")
    token_cost = Column(Float, nullable=True, default=0.0, comment="API 调用成本")

    project = relationship("ContentItem", back_populates="summary")

    __table_args__ = (
        Index("idx_project_id", "project_id"),
        Index("idx_score", "score"),
        UniqueConstraint("project_id", name="uq_project_summary"),
    )

    def __repr__(self):
        return f"<Summary(id={self.id}, project_id={self.project_id}, score={self.score})>"

    def get_pros_list(self) -> Column[Any] | list[Any] | Any:
        if isinstance(self.pros, str):
            return json.loads(self.pros)
        return self.pros or []

    def get_cons_list(self) -> Column[Any] | list[Any] | Any:
        if isinstance(self.cons, str):
            return json.loads(self.cons)
        return self.cons or []

    def get_tags_list(self) -> Column[Any] | list[Any] | Any:
        if isinstance(self.tags, str):
            return json.loads(self.tags)
        return self.tags or []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "one_liner": self.one_liner,
            "summary": self.summary,
            "use_cases": self.use_cases,
            "pros": self.get_pros_list(),
            "cons": self.get_cons_list(),
            "tags": self.get_tags_list(),
            "score": self.score,
            "install_step": self.install_step,
            "model_used": self.model_used,
            "processing_status": self.processing_status,
            "token_cost": self.token_cost
        }


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    content_item_id = Column(Integer, ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False)

    source = Column(String(50), nullable=False)
    external_id = Column(String(255), nullable=False)
    # NOTE (ADR-0002, 2026-08-06): `category` is intentionally KEPT as a column for
    # per-entity classification (HF pipeline tag / GitHub language) but REMOVED from the
    # daily uniqueness key. One row per entity per day; trend comparison is by external_id.
    category = Column(String(100), nullable=True, default="")

    rank = Column(Integer, nullable=True)
    metrics = Column(JSONB, nullable=False)

    captured_date = Column(Date, nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Unified daily snapshot key: one row per (source, external_id, captured_date).
        UniqueConstraint("source", "external_id", "captured_date", name="uq_snapshot_daily"),
        Index("idx_snapshot_lookup", "source", "external_id", "captured_at"),
        Index("idx_snapshot_category_lookup", "source", "category", "captured_at"),
    )


class NewsCluster(Base):
    """新闻聚类表 - 用于去重同一新闻的多个来源"""
    __tablename__ = "news_clusters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    canonical_item_id = Column(Integer, ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False)
    member_item_ids = Column(JSON, nullable=False, default=[])  # list of ContentItem.id
    cluster_method = Column(String(50), nullable=False)  # "exact_url" | "title_similarity" | "embedding"
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_news_cluster_canonical", "canonical_item_id"),
    )

    def __repr__(self):
        return f"<NewsCluster(id={self.id}, canonical={self.canonical_item_id}, members={len(self.member_item_ids or [])})>"