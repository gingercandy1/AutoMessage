import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from sqlalchemy.dialects.postgresql import insert

from app.api.hackernews_client import HackerNewsClient
from app.api.rss_client import RSSClient, RSSFeedEntry
from app.collectors.base import BaseCollector
from app.collectors.news.news_base import (
    NewsDeduplicator,
    fetch_full_text,
    matches_keywords as _matches_keywords_impl,
    categorize_news as _categorize_news_impl,
)
from app.config import settings
from app.database import SessionLocal
from app.database.models import ContentItem, MetricSnapshot
from app.logger import AppLogger

logger = AppLogger.get(__name__)


@dataclass
class NewsConfig:
    max_items: int = 100
    hn_min_score: int = 50
    hn_keywords: Optional[List[str]] = None
    rss_feeds: Optional[List[str]] = None
    since_hours: int = 24
    dedup_enabled: bool = True

    def __post_init__(self):
        if self.hn_keywords is None:
            self.hn_keywords = [k.strip().lower() for k in settings.news_hn_keywords.split(",")]
        if self.rss_feeds is None:
            self.rss_feeds = [f.strip() for f in settings.news_rss_feeds.split(",") if f.strip()]


class NewsCollector(BaseCollector):
    NAME = "news"

    def __init__(self):
        self.hn_client = HackerNewsClient()
        self.rss_client = RSSClient()
        self._deduper = NewsDeduplicator()

    def _matches_keywords(self, text: str, keywords: List[str]) -> bool:
        """检查文本是否包含关键词（委托共享实现）"""
        return _matches_keywords_impl(text, keywords)

    def _categorize_news(self, title: str, description: str, source: str) -> str:
        """根据标题和描述对新闻进行分类（委托共享实现）"""
        return _categorize_news_impl(title, description, source)

    def _normalize_hn_item(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """标准化 Hacker News 项目"""
        if not item or item.get("type") != "story":
            return None
        
        title = item.get("title", "").strip()
        if not title:
            return None
        
        url = item.get("url", "")
        if not url:
            url = f"https://news.ycombinator.com/item?id={item.get('id')}"
        
        # 过滤：分数太低或不包含关键词
        score = item.get("score", 0)
        if score < settings.news_hn_min_score:
            return None
        
        text = f"{title} {item.get('text', '')}"
        if not self._matches_keywords(text, [k.strip().lower() for k in settings.news_hn_keywords.split(",")]):
            return None
        
        # 作者
        author = item.get("by", "")
        
        # 发布时间
        published_at = None
        if item.get("time"):
            published_at = datetime.fromtimestamp(item["time"], tz=timezone.utc)
        
        # 外部 ID
        external_id = f"hn_{item.get('id')}"
        
        # 分类
        category = self._categorize_news(title, item.get("text", ""), "hacker_news")
        
        return {
            "source": "hacker_news",
            "external_id": external_id,
            "title": title,
            "url": url,
            "description": item.get("text", "")[:500] if item.get("text") else "",
            "author": author,
            "published_at": published_at,
            "category": category,
            "hn_score": score,
            "hn_comments": item.get("descendants", 0),
            "hn_id": item.get("id"),
            "raw_data": item,
        }

    def _normalize_rss_entry(self, entry: RSSFeedEntry, source: str = "news") -> Optional[Dict[str, Any]]:
        """标准化 RSS 条目"""
        if not entry.title or not entry.url:
            return None
        
        # 外部 ID 使用 URL 的哈希
        external_id = f"rss_{hashlib.md5(entry.url.encode()).hexdigest()[:16]}"
        
        # 分类
        category = self._categorize_news(entry.title, entry.description or "", source)
        
        return {
            "source": source,
            "external_id": external_id,
            "title": entry.title,
            "url": entry.url,
            "description": entry.description or "",
            "author": entry.author,
            "published_at": entry.published_at,
            "category": category,
            "feed_url": entry.feed_url,
            "tags": entry.tags or [],
            "raw_data": {
                "title": entry.title,
                "url": entry.url,
                "description": entry.description,
                "author": entry.author,
                "published_at": entry.published_at.isoformat() if entry.published_at else None,
                "feed_url": entry.feed_url,
                "content": entry.content,
                "tags": entry.tags,
                "image_url": entry.image_url,
            },
        }

    async def _fetch_hn_stories(self, config: NewsConfig) -> List[Dict[str, Any]]:
        """获取 Hacker News 故事"""
        logger.info("🔍 获取 Hacker News 热门故事...")
        
        story_ids = await self.hn_client.get_top_stories(limit=500)
        if not story_ids:
            return []
        
        # 批量获取详情
        items = await self.hn_client.get_items_batch(story_ids, max_concurrent=20)
        
        normalized = []
        for item in items:
            if item:
                norm = self._normalize_hn_item(item)
                if norm:
                    normalized.append(norm)
        
        # 按分数排序
        normalized.sort(key=lambda x: x.get("hn_score", 0), reverse=True)
        
        logger.info(f"✅ Hacker News: {len(normalized)} 条符合条件")
        return normalized[:config.max_items]

    async def _fetch_rss_feeds(self, config: NewsConfig) -> List[Dict[str, Any]]:
        """获取 RSS 订阅"""
        rss_feeds = config.rss_feeds or []
        logger.info(f"🔍 抓取 {len(rss_feeds)} 个 RSS 源...")
        
        since = datetime.now(timezone.utc) - timedelta(hours=config.since_hours)
        entries = await self.rss_client.fetch_multiple_feeds(
            rss_feeds,
            since=since,
            max_entries_per_feed=30,
        )
        
        normalized = []
        for entry in entries:
            norm = self._normalize_rss_entry(entry)
            if norm:
                normalized.append(norm)
        
        # 按发布时间排序
        normalized.sort(key=lambda x: x.get("published_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        
        logger.info(f"✅ RSS: {len(normalized)} 条")
        return normalized[:config.max_items]

    def _levenshtein_ratio(self, a, b):
        return self._deduper.levenshtein_ratio(a, b)

    def _normalize_url(self, url: str) -> str:
        return self._deduper.normalize_url(url)

    def _deduplicate(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """去重 + 聚类（Q10）。委托给共享 NewsDeduplicator。"""
        return self._deduper.deduplicate(items)

    @property
    def _last_clusters(self) -> List[List[int]]:
        return self._deduper._last_clusters

    @_last_clusters.setter
    def _last_clusters(self, value: List[List[int]]) -> None:
        self._deduper._last_clusters = value

    def _write_clusters(self, item_id_by_index: Dict[int, int]):
        """委托给共享 NewsDeduplicator 写 NewsCluster。"""
        self._deduper.write_clusters(item_id_by_index)

    async def _fetch_full_text(self, url: str) -> str:
        """抓取并提取文章正文（Q3，委托共享实现）"""
        return await fetch_full_text(url)

    async def _enrich_full_text(self, items: List[Dict[str, Any]]) -> None:
        """采集时为每条新闻缓存全文到 raw_data['full_text']（Q3）。

        RSS 条目若已有 content 则直接复用，不再联网；其余带 URL 的尝试抓取，
        限制并发避免打爆源站。原地修改 items 的 raw_data。
        """
        sem = asyncio.Semaphore(5)

        async def _enrich_one(item: Dict[str, Any]) -> None:
            raw = item.get("raw_data") or {}
            if isinstance(raw, dict) and raw.get("full_text"):
                return
            # RSS 已有正文，直接复用
            if isinstance(raw, dict) and raw.get("content"):
                raw["full_text"] = raw["content"][:8000]
                item["raw_data"] = raw
                return
            url = item.get("url")
            if not url:
                return
            async with sem:
                text = await self._fetch_full_text(url)
            if text:
                if not isinstance(raw, dict):
                    raw = {}
                raw["full_text"] = text
                item["raw_data"] = raw

        await asyncio.gather(*[_enrich_one(it) for it in items])

    async def fetch(self, config: NewsConfig) -> int:
        """抓取新闻"""
        hn_items = await self._fetch_hn_stories(config)
        rss_items = await self._fetch_rss_feeds(config)

        all_items = hn_items + rss_items
        all_items = self._deduplicate(all_items)

        # 限制数量
        all_items = all_items[:config.max_items]

        # Q3: 采集时缓存全文到 raw_data['full_text']，供摘要阶段只读缓存
        await self._enrich_full_text(all_items)

        await self.save(all_items)
        return len(all_items)

    async def save(self, records: List[Dict[str, Any]]) -> None:
        """保存到数据库"""
        db = SessionLocal()
        now = datetime.now(timezone.utc)
        today = now.date()
        item_id_by_index: Dict[int, int] = {}
        
        try:
            for index, item in enumerate(records):
                source = item["source"]
                external_id = item["external_id"]
                
                # 准备 metrics
                published_at = item.get("published_at")
                metrics_data = {
                    "category": item.get("category", "ai"),
                    "published_at": published_at.isoformat() if published_at else None,
                }
                
                if source == "hacker_news":
                    metrics_data.update({
                        "hn_score": item.get("hn_score"),
                        "hn_comments": item.get("hn_comments"),
                        "hn_id": item.get("hn_id"),
                    })
                else:
                    metrics_data.update({
                        "feed_url": item.get("feed_url"),
                        "tags": item.get("tags", []),
                    })
                
                # 检查是否存在
                existing = db.query(ContentItem).filter_by(
                    source=source,
                    external_id=external_id
                ).first()
                
                if existing:
                    existing.title = item["title"]
                    existing.url = item["url"]
                    existing.description = item["description"]
                    existing.author = item.get("author")
                    existing.category = item.get("category")
                    existing.metrics = metrics_data
                    existing.raw_data = item.get("raw_data")
                    content_item = existing
                else:
                    # content_items.url 有独立的唯一约束，(source, external_id) 不
                    # 撞不代表 url 不撞——hnrss.org/newest 本身就是 Hacker News 的镜
                    # 像，会把 HackerNewsClient 直采到的同一条又以 rss_<md5> 的
                    # external_id 再收一次，url 相同。撞了 unique 约束会在这个
                    # db.flush() 抛异常，导致本方法一次没 commit 的整批全部回滚——
                    # 表现为"新闻采集完成: 0 条"，看着像没抓到东西，其实是抓到了
                    # 但存不进去。这里先按 url 查一次，撞了就复用已有行，不重复插入。
                    existing_by_url = db.query(ContentItem).filter_by(url=item["url"]).first()
                    if existing_by_url:
                        content_item = existing_by_url
                    else:
                        content_item = ContentItem(
                            source=source,
                            external_id=external_id,
                            title=item["title"],
                            url=item["url"],
                            description=item["description"],
                            author=item.get("author"),
                            category=item.get("category"),
                            discovered_at=item.get("published_at") or now,
                            processed=0,
                            metrics=metrics_data,
                            raw_data=item.get("raw_data"),
                        )
                        db.add(content_item)
                        db.flush()

                item_id_by_index[index] = content_item.id
                
                # 保存 MetricSnapshot（ADR-0002 键：(source, external_id, captured_date)）
                stmt = insert(MetricSnapshot).values(
                    content_item_id=content_item.id,
                    source=source,
                    external_id=external_id,
                    category=item.get("category", "ai"),
                    rank=None,
                    metrics=metrics_data,
                    captured_date=today,
                    captured_at=now,
                ).on_conflict_do_update(
                    index_elements=["source", "external_id", "captured_date"],
                    set_={
                        "content_item_id": content_item.id,
                        "rank": None,
                        "metrics": metrics_data,
                        "captured_at": now,
                    }
                )
                db.execute(stmt)
            
            # 写入新闻聚类（Q10）
            self._write_clusters(item_id_by_index)

            db.commit()
            logger.info(f"✅ 新闻入库完成")
        finally:
            db.close()

    def configs(self):
        return [NewsConfig(
            max_items=settings.news_collection_limit,
            hn_min_score=settings.news_hn_min_score,
            rss_feeds=[f.strip() for f in settings.news_rss_feeds.split(",") if f.strip()],
            since_hours=24,
            dedup_enabled=settings.news_dedup_enabled,
        )]


async def main():
    config = NewsConfig(max_items=50)
    collector = NewsCollector()
    items = await collector.fetch(config)
    print(f"Fetched: {len(items)} items")


if __name__ == "__main__":
    asyncio.run(main())