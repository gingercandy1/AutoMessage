from datetime import date, timedelta

from sqlalchemy import desc, func
from sqlalchemy.orm import Session, joinedload

from app.database import SessionLocal
from app.database.models import ContentItem, UserFavorite, Summary


class GithubService:
    NAME = "github"

    def get_total_hot(self, limit: int = 8) -> list[type[ContentItem]] | None:
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                (ContentItem.source == self.NAME) &
                (ContentItem.metrics["search_type"].as_string() == "stars")
            ) \
            .order_by(desc(ContentItem.metrics["stars"].as_integer())) \
            .limit(limit) \
            .all()
        finally:
            db.close()

    def get_recent_hot(self, day: int = 8) -> list[type[ContentItem]]:
        db: Session = SessionLocal()
        try:
            cutoff_date = date.today() - timedelta(days=day)
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter((func.date(ContentItem.discovered_at) >= cutoff_date) &
                        (ContentItem.metrics["search_type"].as_string()== "stars")) \
                .order_by(desc(ContentItem.metrics["stars"].as_integer())) \
                .all()
        finally:
            db.close()

    def get_trend(self, day: int = 8) -> list[type[ContentItem]]:
        db: Session = SessionLocal()
        try:
            cutoff_date = date.today() - timedelta(days=day)
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                (ContentItem.source == self.NAME) &
                (func.date(ContentItem.discovered_at) >= cutoff_date) &
                (ContentItem.metrics["search_type"].as_string() == "trending")
            ) \
            .order_by(desc(ContentItem.metrics["stars"].as_integer())) \
            .all()
        finally:
            db.close()

    def get_by_language(self, language: str, limit: int = 8) -> list[type[ContentItem]]:
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                (ContentItem.source == self.NAME) &
                (func.lower(ContentItem.metrics["language"].as_string()) == language.lower())
            ) \
            .order_by(desc(ContentItem.metrics["stars"].as_integer())) \
            .limit(limit).all()
        finally:
            db.close()

    def get_by_tag(self, tag: str, limit: int = 8) -> list[type[ContentItem]]:
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .join(Summary, Summary.project_id == ContentItem.id) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                (ContentItem.source == self.NAME) &
                (Summary.summary.ilike(f"%{tag}%"))
            ) \
            .order_by(desc(ContentItem.metrics["stars"].as_integer())) \
            .limit(limit).all()
        finally:
            db.close()

    def get_user_favorites(self, telegram_id: int, limit: int = 10) -> list[type[ContentItem]]:
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .join(UserFavorite, UserFavorite.project_id == ContentItem.id) \
                .filter(
                (ContentItem.source == self.NAME) &
                (UserFavorite.telegram_id == telegram_id)
            ) \
            .order_by(desc(UserFavorite.created_at)) \
            .limit(limit).all()
        finally:
            db.close()

    def search_project(self, repo: str) -> type[ContentItem] | None:
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                (ContentItem.source == self.NAME) &
                (ContentItem.external_id == repo)
            ).first()
        finally:
            db.close()

    def search_projects(self, keyword: str, limit: int = 8) -> list[type[ContentItem]]:
        db: Session = SessionLocal()
        try:
            return db.query(ContentItem) \
                .options(joinedload(ContentItem.summary)) \
                .filter(
                (ContentItem.source == self.NAME) &
                (
                        (ContentItem.title.ilike(f"%{keyword}%")) |
                        (ContentItem.author.ilike(f"%{keyword}%"))
                )
            ) \
            .order_by(desc(ContentItem.metrics["stars"].as_integer())) \
            .limit(limit).all()
        finally:
            db.close()

    def toggle_favorite(self, telegram_id: int, project_id: int) -> dict:
        db: Session = SessionLocal()
        try:
            existing = db.query(UserFavorite)\
                        .filter_by(telegram_id=telegram_id, project_id=project_id)\
                        .first()
            if existing:
                db.delete(existing)
                db.commit()
                return {"action": "removed", "success": True}
            else:
                favorite = UserFavorite(
                    telegram_id=telegram_id,
                    project_id=project_id
                )
                db.add(favorite)
                db.commit()
                return {"action": "added", "success": True}
        finally:
            db.close()



if __name__ == '__main__':
    service = GithubService()
    projects = service.get_trend()

    print(projects)