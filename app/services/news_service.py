from datetime import date, timedelta
from typing import List, Optional

from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session, joinedload

from app.collectors.news_base import NEWS_SOURCES
from app.database import SessionLocal
from app.database.models import ContentItem, UserFavorite, Summary


class NewsService:
    NAME = "news"
    CATEGORIES = ["ai", "llm", "research", "tools", "industry"]
    CATEGORY_LABELS = {
        "ai": "🤖 AI/机器学习",
        "llm": "🧠 大语言模型",
        "research": "📄 研究论文",
        "tools": "🔧 工具/框架",
        "industry": "🏢 行业动态",
    }
    SOURCE_LABELS = {
        "hacker_news": "🟠 HN",
        "news": "📰 News",
        "reddit": "🔴 Reddit",
        "producthunt": "🚀 Product Hunt",
        "official_blog": "🏢 官方博客",
        "hf_papers": "📄 HF Papers",
    }

    #: 属于“新闻”的 source 值（由各采集器注册到 NEWS_SOURCES 后自动生效）
    @property
    def news_sources(self):
        return list(NEWS_SOURCES)

    def get_latest(self, limit: int = 100) -> List[ContentItem]:
        """最新新闻（按发现时间）"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(ContentItem.source.in_(self.news_sources)) \
                .order_by(desc(ContentItem.discovered_at)) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def get_by_category(self, category: str, limit: int = 100) -> List[ContentItem]:
        """按分类获取"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                    (ContentItem.source.in_(self.news_sources)) &
                    (ContentItem.category == category)
                ) \
                .order_by(desc(ContentItem.discovered_at)) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def get_hacker_news(self, limit: int = 100) -> List[ContentItem]:
        """仅 Hacker News"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                    (ContentItem.source == "hacker_news")
                ) \
                .order_by(desc(ContentItem.metrics["hn_score"].as_integer())) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def get_recent_hot(self, days: int = 7, limit: int = 100) -> List[ContentItem]:
        """近期热门（按 HN 分数或发现时间）"""
        db: Session = SessionLocal()
        try:
            cutoff_date = date.today() - timedelta(days=days)
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                    (ContentItem.source.in_(self.news_sources)) &
                    (func.date(ContentItem.discovered_at) >= cutoff_date)
                ) \
                .order_by(desc(ContentItem.metrics["hn_score"].as_integer())) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def get_total_hot(self, limit: int = 200) -> List[ContentItem]:
        """长期热门"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(ContentItem.source.in_(self.news_sources)) \
                .order_by(desc(ContentItem.metrics["hn_score"].as_integer())) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def get_trend(self, days: int = 3, limit: int = 100) -> List[ContentItem]:
        """趋势（近期发布）"""
        db: Session = SessionLocal()
        try:
            cutoff_date = date.today() - timedelta(days=days)
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                    (ContentItem.source.in_(self.news_sources)) &
                    (func.date(ContentItem.discovered_at) >= cutoff_date)
                ) \
                .order_by(desc(ContentItem.discovered_at)) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def get_user_favorites(self, telegram_id: int, limit: int = 100) -> List[ContentItem]:
        """用户收藏"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .join(UserFavorite, UserFavorite.project_id == ContentItem.id) \
                .filter(
                    (ContentItem.source.in_(self.news_sources)) &
                    (UserFavorite.telegram_id == telegram_id)
                ) \
                .order_by(desc(UserFavorite.created_at)) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def search(self, keyword: str, limit: int = 100) -> List[ContentItem]:
        """搜索"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                    (ContentItem.source.in_(self.news_sources)) &
                    (
                        (ContentItem.title.ilike(f"%{keyword}%")) |
                        (ContentItem.author.ilike(f"%{keyword}%")) |
                        (ContentItem.description.ilike(f"%{keyword}%"))
                    )
                ) \
                .order_by(desc(ContentItem.discovered_at)) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def get_unreviewed_news(self, limit: int = 50) -> List[ContentItem]:
        """待审查的新闻：AI 已摘要(processed=1) 且尚未送审(review_status IS NULL)。Q4。"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                    (ContentItem.source.in_(self.news_sources)) &
                    (ContentItem.processed == 1) &
                    (ContentItem.review_status.is_(None))
                ) \
                .order_by(desc(ContentItem.discovered_at)) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def get_approved_news(self, limit: int = 50) -> List[ContentItem]:
        """已审查通过待发布的新闻（review_status='approved'）。Q4。"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                    (ContentItem.source.in_(self.news_sources)) &
                    (ContentItem.review_status == "approved")
                ) \
                .order_by(desc(ContentItem.discovered_at)) \
                .limit(limit) \
                .all()
        finally:
            db.close()

    def set_review_status(self, item_id: int, status: str) -> bool:
        db: Session = SessionLocal()
        try:
            item = db.query(ContentItem).filter(ContentItem.id == item_id).first()
            if not item:
                return False
            item.review_status = status
            db.commit()
            return True
        finally:
            db.close()

    def get_by_id(self, item_id: int) -> Optional[ContentItem]:
        """按 ID 获取"""
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                    (ContentItem.source.in_(self.news_sources)) &
                    (ContentItem.id == item_id)
                ) \
                .first()
        finally:
            db.close()

    def toggle_favorite(self, telegram_id: int, item_id: int) -> dict:
        """切换收藏"""
        db: Session = SessionLocal()
        try:
            existing = db.query(UserFavorite) \
                .filter_by(telegram_id=telegram_id, project_id=item_id) \
                .first()
            if existing:
                db.delete(existing)
                db.commit()
                return {"action": "removed", "success": True}
            else:
                favorite = UserFavorite(
                    telegram_id=telegram_id,
                    project_id=item_id
                )
                db.add(favorite)
                db.commit()
                return {"action": "added", "success": True}
        finally:
            db.close()

    def get_categories(self) -> List[str]:
        """获取所有分类"""
        return self.CATEGORIES

    def get_category_label(self, category: str) -> str:
        return self.CATEGORY_LABELS.get(category, category)

    def get_source_label(self, source: str) -> str:
        return self.SOURCE_LABELS.get(source, source)


if __name__ == "__main__":
    service = NewsService()
    items = service.get_latest(limit=5)
    for item in items:
        print(f"{item.title} ({item.category})")