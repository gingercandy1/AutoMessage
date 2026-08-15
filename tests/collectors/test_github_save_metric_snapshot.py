"""Regression: GitHubCollector.save() must set MetricSnapshot.content_item_id correctly
when the entity already exists (existing branch), not leave it NULL.

Reproduces the NotNullViolation bug:
  null value in column "content_item_id" of relation "metric_snapshots"
which occurred because save() referenced item.id (the passed-in detached object,
which has no id in the existing branch) instead of the persisted entity's id.
"""
import asyncio
import pytest

from app.database import SessionLocal
from app.database.models import ContentItem, MetricSnapshot
from app.collectors.github_collector import GitHubTrendingCollector


def _make_record(external_id: str) -> ContentItem:
    return ContentItem(
        source="github",
        external_id=external_id,
        title="demo",
        url=f"https://github.com/{external_id}",
        description="d",
        author="owner",
        metrics={"stars": 1, "forks": 0, "language": "Python"},
        raw_data={"language": "Python"},
    )


def test_save_existing_entity_writes_metric_snapshot_with_content_item_id():
    collector = GitHubTrendingCollector()
    ext = "octocat/Hello-World"

    db = SessionLocal()
    try:
        # 预置一个已存在的实体（模拟库里已有该 repo）
        existing = ContentItem(
            source="github", external_id=ext, title="demo",
            url=f"https://github.com/{ext}", description="d", author="owner",
            metrics={"stars": 1, "forks": 0, "language": "Python"}, raw_data={"language": "Python"},
        )
        db.add(existing)
        db.flush()
        existing_id = existing.id
        db.commit()
        assert existing_id is not None

        # 传入一个 detached 记录（无 id），external_id 相同 -> 走 existing 分支
        record = _make_record(ext)
        assert record.id is None  # 确认传入对象确实无 id

        added = asyncio.run(collector.save([record]))  # 之前这里会因 content_item_id=null 抛错

        snap = db.query(MetricSnapshot).filter_by(
            source="github", external_id=ext
        ).order_by(MetricSnapshot.captured_at.desc()).first()
        assert snap is not None
        assert snap.content_item_id == existing_id  # 关键：指向已存在实体，不是 null
    finally:
        # 清理
        db.query(MetricSnapshot).filter_by(source="github", external_id=ext).delete()
        db.query(ContentItem).filter_by(source="github", external_id=ext).delete()
        db.commit()
        db.close()


def test_save_new_entity_writes_metric_snapshot():
    collector = GitHubTrendingCollector()
    ext = "octocat/New-Repo"

    db = SessionLocal()
    try:
        record = _make_record(ext)
        added = asyncio.run(collector.save([record]))
        assert added == 1

        # 从库取回该实体，确认其 id 已写入 MetricSnapshot
        saved = db.query(ContentItem).filter_by(
            source="github", external_id=ext
        ).first()
        assert saved is not None

        snap = db.query(MetricSnapshot).filter_by(
            source="github", external_id=ext
        ).order_by(MetricSnapshot.captured_at.desc()).first()
        assert snap is not None
        assert snap.content_item_id == saved.id
    finally:
        db.query(MetricSnapshot).filter_by(source="github", external_id=ext).delete()
        db.query(ContentItem).filter_by(source="github", external_id=ext).delete()
        db.commit()
        db.close()
