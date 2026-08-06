from datetime import timedelta, date, datetime, timezone
from typing import Optional

from sqlalchemy import desc, func, or_, literal, and_, Date, cast
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.database.models import UserFavorite, ContentItem


class HuggingFaceService:
    NAME = "huggingface"

    @staticmethod
    def _latest_fetched_at(db: Session, category_key: str="") -> Optional[datetime]:
        """
        取得该分类『最近一次』抓取的时间点。
        不直接假设"今天一定有数据"——采集任务可能延迟、跳过一天，
        或者服务器和抓取脚本不在同一时区，用这个更稳。
        """
        subq = (
            db.query(func.max(ContentItem.discovered_at))
            .filter(
                or_(
                    ContentItem.metrics["category"].as_string() == category_key,
                    literal(category_key is None).is_(True),
                    literal(category_key == "").is_(True)
                )
            )
            .filter(
                ContentItem.source == HuggingFaceService.NAME
            )
            .scalar()
        )

        max_date = db.query(cast(subq, Date)).scalar()
        return max_date

    @staticmethod
    def _ranking_at(db: Session, category_key: str, fetched_at: datetime, limit: int):
        return (
            db.query(ContentItem)
            .filter(
                    or_(
                        ContentItem.metrics["category"].as_string() == category_key,
                        literal(category_key is None).is_(True),
                        literal(category_key == "").is_(True)
                    )
            )
            .filter(cast(ContentItem.discovered_at, Date) == fetched_at)
            .filter(ContentItem.source == HuggingFaceService.NAME)
            .order_by(ContentItem.metrics["rank"].as_integer().asc())
            .limit(limit)
            .all()
        )


    def get_ranking_days_ago(self, category_key: str, days_ago: int = 7, limit: int = 10):
        """取得『N 天前』该类别的排行快照，用来跟今天比较变化"""
        db: Session = SessionLocal()
        try:
            target_date = date.today() - timedelta(days=days_ago)
            day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
            day_end = day_start + timedelta(days=1)

            return (
                db.query(ContentItem)
                .filter(
                    ContentItem.source == self.NAME,
                    ContentItem.metrics["category"].as_string() == category_key,
                    ContentItem.discovered_at >= day_start,
                    ContentItem.discovered_at < day_end
                )
                .order_by(ContentItem.metrics["rank"].as_integer().asc())
                .limit(limit)
                .all()
            )
        finally:
            db.close()

    def get_latest_ranking(self, category_key: str="", limit: int = 10):
        """取该分类『最近一次抓取』的排行榜"""
        db: Session = SessionLocal()
        try:
            latest = self._latest_fetched_at(db, category_key)
            if not latest:
                return []
            return self._ranking_at(db, category_key, latest, limit)
        finally:
            db.close()

    def get_ranking_with_trend(self, category_key: str, limit: int = 10, compare_days_ago: int = 1):
        """在最新排行榜基础上，附加每个模型相比 N 天前的名次变化。"""
        latest = self.get_latest_ranking(category_key, limit=limit)
        if not latest:
            return []

        previous = self.get_ranking_days_ago(category_key, days_ago=compare_days_ago, limit=1000)
        previous_rank_map = {m.model_id: m.rank for m in previous}

        results = []
        for m in latest:
            prev_rank = previous_rank_map.get(m.model_id)
            rank_change = (prev_rank - m.rank) if prev_rank is not None else None
            results.append({
                "model": m,
                "rank_change": rank_change,
                "is_new": prev_rank is None,
            })
        return results

    def get_total_hot(self, category_key: str="", limit: int = 200):
        """长期热门：同样用最新快照，但改按 likes 排序，体现长期积累的热度而非短期波动"""
        db: Session = SessionLocal()
        try:
            latest = self._latest_fetched_at(db, category_key)
            if not latest:
                return []
            return (
                db.query(ContentItem)
                .filter(
                    or_(
                        ContentItem.metrics["category"].as_string() == category_key,
                        literal(category_key is None).is_(True),
                        literal(category_key == "").is_(True)
                    )
                )
                .filter(cast(ContentItem.discovered_at, Date) == latest)
                .filter(ContentItem.source == self.NAME)
                .order_by(desc(ContentItem.metrics["likes"]))
                .limit(limit)
                .all()
            )
        finally:
            db.close()

    def get_trend(self, category_key: str="", limit: int = 100):
        return self.get_latest_ranking(category_key, limit=limit)

    def get_all_categories(self) -> list[str]:
        """返回目前数据库里实际有数据的分类（用于动态生成"更多分类"菜单，比写死 PIPELINE_MAP 更贴近真实数据）"""
        db: Session = SessionLocal()
        try:
            rows = (
                db.query(ContentItem.metrics["category"].as_string())
                .filter(ContentItem.source == self.NAME)
                .distinct()
                .all()
            )
            return [r[0] for r in rows if r[0] is not None]
        finally:
            db.close()

    def search_models(self, keyword: str, limit: int = 100):
        """
        在『每个分类各自最新一次快照』里模糊搜索 model_id。
        不同分类的最新抓取时间可能不同，所以用子查询各自取各自的 max(fetched_at)，
        而不是用一个全局的 latest 时间去过滤（那样会漏掉抓取较早的分类）。
        """
        db: Session = SessionLocal()
        try:
            like_pattern = f"%{keyword}%"

            # 建立每個 category 的最新時間子查詢
            latest_per_category = (
                db.query(
                    ContentItem.metrics["category"].as_string().label("category"),
                    func.max(ContentItem.discovered_at).label("max_discovered_at"),
                )
                .filter(ContentItem.source == self.NAME)
                .group_by(ContentItem.metrics["category"].as_string())
                .subquery()
            )

            return (
                db.query(ContentItem)
                .join(
                    latest_per_category,
                    and_(
                        ContentItem.metrics["category"].as_string() == latest_per_category.c.category,
                        ContentItem.discovered_at == latest_per_category.c.max_discovered_at
                    )
                )
                .filter(
                    ContentItem.source == self.NAME,
                    ContentItem.external_id.ilike(like_pattern)
                )
                # 這裡假設熱度權重存放在 metrics 的 trending_score 中
                .order_by(ContentItem.metrics["trending_score"].as_integer().desc())
                .limit(limit)
                .all()
            )
        finally:
            db.close()

    def get_model_history(self, model_id: str, category_key: str, days: int = 30):
        """某个模型在某分类下最近 N 天的 rank/likes/downloads 变化，用于画趋势图或模型详情页"""
        db: Session = SessionLocal()
        try:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            return (
                db.query(ContentItem)
                .filter(
                    ContentItem.source == self.NAME,
                    ContentItem.external_id == model_id,
                    ContentItem.metrics["category"].as_string() == category_key,
                    ContentItem.discovered_at >= since
                )
                .order_by(ContentItem.discovered_at.asc())
                .all()
            )
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

            # 找出這些收藏對象中，各個 model 的最新一筆 external_id 快照
            # 因為收藏時我們記錄了特定的 item_id，但用戶想看的是該模型「最新」的數據
            distinct_models = (
                db.query(ContentItem.external_id)
                .filter(ContentItem.id.in_(item_ids))
                .distinct()
                .all()
            )
            model_ids = [m[0] for m in distinct_models]

            latest_per_model = (
                db.query(
                    ContentItem.external_id.label("model_id"),
                    func.max(ContentItem.discovered_at).label("max_discovered_at"),
                )
                .filter(ContentItem.source == self.NAME, ContentItem.external_id.in_(model_ids))
                .group_by(ContentItem.external_id)
                .subquery()
            )

            return (
                db.query(ContentItem)
                .join(
                    latest_per_model,
                    and_(
                        ContentItem.external_id == latest_per_model.c.model_id,
                        ContentItem.discovered_at == latest_per_model.c.max_discovered_at
                    )
                ).all()
            )
        finally:
            db.close()

    def toggle_favorite(self, telegram_id: int, model_id: str) -> dict:
        """
        切換收藏狀態。
        注意：由於 ContentItem 是快照式設計（同一 model_id 每天都有一條新記錄），
        收藏時我們應錨定它最新的那條 ContentItem.id，或者根據 external_id 判斷。
        """
        db: Session = SessionLocal()
        try:
            # 先找該模型最新的一條記錄
            latest_item = (
                db.query(ContentItem)
                .filter(ContentItem.source == self.NAME, ContentItem.external_id == model_id)
                .order_by(ContentItem.discovered_at.desc())
                .first()
            )
            if not latest_item:
                return {"action": "error", "message": "Model not found"}

            # 檢查是否已收藏（透過檢查該用戶是否收藏過這個 external_id 關聯的任何快照）
            existing = (
                db.query(UserFavorite)
                .join(ContentItem, UserFavorite.project_id == ContentItem.id)
                .filter(
                    UserFavorite.telegram_id == telegram_id,
                    ContentItem.source == self.NAME,
                    ContentItem.external_id == model_id
                )
                .first()
            )

            if existing:
                db.delete(existing)
                db.commit()
                return {"action": "removed"}

            # 新增收藏，綁定當前最新的一條 ContentItem.id (ADR-0002: project_id)
            db.add(UserFavorite(telegram_id=telegram_id, project_id=latest_item.id))
            db.commit()
            return {"action": "added"}
        finally:
            db.close()


if __name__ == '__main__':
    service = HuggingFaceService()
    trend = service.get_trend()
    print(trend)