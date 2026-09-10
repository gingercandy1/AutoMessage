from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session, joinedload

from app.database import SessionLocal
from app.database.models import UserFavorite, ContentItem


class HuggingFaceService:
    NAME = "huggingface"

    @staticmethod
    def _dedupe_by_model_name(items: list[ContentItem]) -> list[ContentItem]:
        """按模型名（external_id 去掉 author 前缀）去重，保留 likes 最高的一条。

        HF trending 上同一模型常被多个用户镜像上传（如 N 个 org 各传一份
        Qwen3.8-27B），列表页只显示 name 时看起来就是重复条目。
        """
        best: dict[str, ContentItem] = {}
        order: list[str] = []
        for m in items:
            name = (m.external_id or "").split("/")[-1].lower()
            if not name:
                continue
            if name not in best:
                best[name] = m
                order.append(name)
            else:
                cur = best[name]
                if (m.metrics or {}).get("likes", 0) > (cur.metrics or {}).get("likes", 0):
                    best[name] = m
        return [best[n] for n in order]

    def get_latest_ranking(self, pipeline_tag: str = "", limit: int = 10) -> list[ContentItem]:
        """该分类的当前排行（ADR-0002 单行 upsert，ContentItem 即最新快照）。"""
        db: Session = SessionLocal()
        try:
            q = db.query(ContentItem).options(joinedload(ContentItem.summary)).filter(ContentItem.source == self.NAME)
            if pipeline_tag:
                q = q.filter(
                    ContentItem.metrics["pipeline_tag"].as_string() == pipeline_tag
                )
            return self._dedupe_by_model_name(
                q.order_by(ContentItem.metrics["rank"].as_integer().asc())
                .limit(limit)
                .all()
            )[:limit]
        finally:
            db.close()

    def get_total_hot(self, pipeline_tag: str = "", limit: int = 200) -> list[ContentItem]:
        """长期热门：按 likes 排序，体现长期积累的热度。"""
        db: Session = SessionLocal()
        try:
            q = db.query(ContentItem).options(joinedload(ContentItem.summary)).filter(ContentItem.source == self.NAME)
            if pipeline_tag:
                q = q.filter(
                    ContentItem.metrics["pipeline_tag"].as_string() == pipeline_tag
                )
            items = (
                q.order_by(desc(ContentItem.metrics["likes"].as_integer()))
                .limit(limit * 2)
                .all()
            )
            return self._dedupe_by_model_name(items)[:limit]
        finally:
            db.close()

    def get_trend(self, pipeline_tag: str = "", limit: int = 100):
        return self.get_latest_ranking(pipeline_tag, limit=limit)

    def search_models(self, keyword: str, limit: int = 100):
        """按 external_id 模糊搜索，按 trending_score 排序。

        注：旧实现按「每分类最新快照时间」join——那是快照式设计
        （每日多行）的产物；ADR-0002 下每模型单行，直接全量搜索即可。
        """
        db: Session = SessionLocal()
        try:
            like_pattern = f"%{keyword}%"
            items = (
                db.query(ContentItem)
                .options(joinedload(ContentItem.summary))
                .filter(
                    ContentItem.source == self.NAME,
                    ContentItem.external_id.ilike(like_pattern),
                )
                .order_by(
                    desc(ContentItem.metrics["trending_score"].as_integer())
                )
                .limit(limit)
                .all()
            )
            return self._dedupe_by_model_name(items)[:limit]
        finally:
            db.close()

    def get_recently_updated(self, limit: int = 100, days: int = 30) -> list[type[ContentItem]]:
        """近期被活跃维护的模型（ADR-0001 §4 更新趋势）。

        按 metrics.last_modified 倒序取最近 days 天内有更新的模型。
        注意：ADR-0002 下每个模型单行 upsert，故用 last_modified 时间排序，
        而非 search_type 标记（单行无法同时标记 hot+trend）。
        """
        from datetime import timedelta
        db: Session = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            items = (
                db.query(ContentItem)
                .options(joinedload(ContentItem.summary))
                .filter(ContentItem.source == self.NAME)
                .filter(ContentItem.metrics["last_modified"].as_string().isnot(None))
                .filter(ContentItem.metrics["last_modified"].as_string() >= cutoff.isoformat())
                .order_by(ContentItem.metrics["last_modified"].as_string().desc())
                .limit(limit)
                .all()
            )
            return self._dedupe_by_model_name(items)[:limit]
        finally:
            db.close()

    def get_user_favorites(self, telegram_id: int):
        """取得用戶收藏的模型最新快照"""
        db: Session = SessionLocal()
        try:
            fav_rows = (
                db.query(UserFavorite.project_id)  # ADR-0002: UserFavorite anchors ContentItem.id via project_id
                .filter(UserFavorite.telegram_id == telegram_id)
                .all()
            )
            item_ids = [row[0] for row in fav_rows]
            if not item_ids:
                return []

            # ADR-0002 单行设计：收藏锚定的 ContentItem.id 即该模型唯一行
            return (
                db.query(ContentItem)
                .options(joinedload(ContentItem.summary))
                .filter(ContentItem.id.in_(item_ids))
                .all()
            )
        finally:
            db.close()

    def toggle_favorite(self, telegram_id: int, model_id: str) -> dict:
        """切換收藏狀態。ADR-0002：ContentItem 每模型单行，直接锚定该行。"""
        db: Session = SessionLocal()
        try:
            latest_item = (
                db.query(ContentItem)
                .filter(ContentItem.source == self.NAME, ContentItem.external_id == model_id)
                .first()
            )
            if not latest_item:
                return {"action": "error", "message": "Model not found"}

            existing = (
                db.query(UserFavorite)
                .filter(
                    UserFavorite.telegram_id == telegram_id,
                    UserFavorite.project_id == latest_item.id,
                )
                .first()
            )

            if existing:
                db.delete(existing)
                db.commit()
                return {"action": "removed"}

            db.add(UserFavorite(telegram_id=telegram_id, project_id=latest_item.id))
            db.commit()
            return {"action": "added"}
        finally:
            db.close()


if __name__ == '__main__':
    service = HuggingFaceService()
    trend = service.get_trend()
    print(trend)
