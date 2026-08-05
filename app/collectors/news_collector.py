import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Set

from sqlalchemy.dialects.postgresql import insert

from app.api.hackernews_client import HackerNewsClient
from app.api.rss_client import RSSClient, RSSFeedEntry
from app.collectors.base import BaseCollector
from app.config import settings
from app.database import SessionLocal
from app.database.models import ContentItem, MetricSnapshot, NewsCluster
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

    def _matches_keywords(self, text: str, keywords: List[str]) -> bool:
        """检查文本是否包含关键词"""
        text_lower = text.lower()
        # 支持词组匹配：检查完整短语和单个词
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in text_lower:
                return True
            # 如果关键词包含空格，也尝试拆分匹配
            if " " in kw_lower:
                parts = kw_lower.split()
                if all(part in text_lower for part in parts):
                    return True
        return False

    def _categorize_news(self, title: str, description: str, source: str) -> str:
        """根据标题和描述对新闻进行分类"""
        text = f"{title} {description}".lower()
        
        # LLM/大模型相关 - 放在最前面，优先匹配
        if any(kw in text for kw in ["llm", "large language model", "gpt", "claude", "llama", "mistral", "gemini", "fine-tun", "instruct", "chat"]):
            return "llm"
        
        # 研究论文相关
        if any(kw in text for kw in ["arxiv", "paper", "research", "study", "benchmark", "evaluation"]):
            return "research"
        
        # AI 通用
        if any(kw in text for kw in ["ai", "artificial intelligence", "machine learning", "deep learning", "neural", "transformer", "generative"]):
            return "ai"
        
        # 工具/框架
        if any(kw in text for kw in ["tool", "framework", "library", "sdk", "api", "github", "open source", "release", "launch"]):
            return "tools"
        
        # 行业/商业
        if any(kw in text for kw in ["funding", "investment", "acquisition", "startup", "company", "enterprise", "business", "market"]):
            return "industry"
        
        return "ai"  # 默认分类

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
            },
        }

    async def _fetch_hn_stories(self, config: NewsConfig) -> List[Dict[str, Any]]:
        """获取 Hacker News 故事"""
        logger.info("🔍 获取 Hacker News 热门故事...")
        
        story_ids = await self.hn_client.get_top_stories(limit=500)
        if not story_ids:
            return []
        
        # 批量获取详情
        items = await self.hn_client.get_items_batch(story_ids[:200], max_concurrent=20)
        
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

    def _deduplicate(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """去重：基于 URL 和标题相似度"""
        if not settings.news_dedup_enabled:
            return items
        
        seen_urls: Set[str] = set()
        seen_titles: Dict[str, Dict[str, Any]] = {}  # 简化：只用标题前 50 字符
        unique = []
        
        for item in items:
            url = item.get("url", "").strip()
            title = item.get("title", "").strip()[:50].lower()
            
            # URL 去重
            if url and url in seen_urls:
                continue
            
            # 标题去重（简单版本）
            if title and title in seen_titles:
                continue
            
            if url:
                seen_urls.add(url)
            if title:
                seen_titles[title] = item
            
            unique.append(item)
        
        logger.info(f"🔄 去重: {len(items)} -> {len(unique)}")
        return unique

    async def fetch(self, config: NewsConfig) -> list[dict[str, Any]]:
        """抓取新闻"""
        hn_items = await self._fetch_hn_stories(config)
        rss_items = await self._fetch_rss_feeds(config)
        
        all_items = hn_items + rss_items
        all_items = self._deduplicate(all_items)
        
        # 限制数量
        all_items = all_items[:config.max_items]
        
        await self.save(all_items)
        return all_items

    async def save(self, records: List[Dict[str, Any]]) -> None:
        """保存到数据库"""
        db = SessionLocal()
        now = datetime.now(timezone.utc)
        today = now.date()
        
        try:
            for item in records:
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
                
                # 保存 MetricSnapshot
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
                    index_elements=["source", "external_id", "category", "captured_date"],
                    set_={
                        "content_item_id": content_item.id,
                        "rank": None,
                        "metrics": metrics_data,
                        "captured_at": now,
                    }
                )
                db.execute(stmt)
            
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