"""Reddit 新闻采集器（T02）。

从 Reddit 公开 .json 接口抓取配置的子版块（默认 LocalLLaMA/singularity/OpenAI/artificial），
按 AI 关键词 + 热度门槛过滤，以真实 source 值 `reddit` 落库，纳入跨源去重与审查链路。
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from app.api.reddit_client import RedditClient
from app.collectors.base import BaseCollector
from app.collectors.news_base import (
    NewsDeduplicator,
    save_news_items,
    register_news_source,
    matches_keywords,
    categorize_news,
)
from app.config import settings
from app.logger import AppLogger

logger = AppLogger.get(__name__)

register_news_source("reddit")


@dataclass
class RedditConfig:
    subreddits: Optional[List[str]] = None
    min_score: int = 50
    since_hours: int = 72
    limit_per_sub: int = 50
    max_items: int = 100

    def __post_init__(self):
        if self.subreddits is None:
            self.subreddits = [
                s.strip() for s in settings.reddit_subreddits.split(",") if s.strip()
            ]
        if self.min_score <= 0:
            self.min_score = settings.reddit_min_score


class RedditCollector(BaseCollector):
    NAME = "reddit"

    def __init__(self):
        self.client = RedditClient()
        self._deduper = NewsDeduplicator()
        self._keywords = [k.strip().lower() for k in settings.news_hn_keywords.split(",")]

    def _normalize_post(self, post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """标准化 Reddit 帖子为统一新闻 record。"""
        if not isinstance(post, dict):
            return None

        title = (post.get("title") or "").strip()
        if not title:
            return None
        if post.get("removed_by_category") or post.get("stickied"):
            return None

        score = post.get("score", 0) or 0
        if score < self.config_min_score:
            return None

        text = f"{title} {post.get('selftext', '')}"
        if not matches_keywords(text, self._keywords):
            return None

        url = post.get("url") or ""
        if not url:
            url = f"https://www.reddit.com{post.get('permalink', '')}"

        published_at = None
        created_utc = post.get("created_utc")
        if created_utc:
            published_at = datetime.fromtimestamp(created_utc, tz=timezone.utc)

        return {
            "source": "reddit",
            "external_id": f"reddit_{post.get('id')}",
            "title": title,
            "url": url,
            "description": (post.get("selftext") or "")[:500],
            "author": post.get("author"),
            "published_at": published_at,
            "category": categorize_news(title, post.get("selftext", ""), "reddit"),
            "score": score,
            "num_comments": post.get("num_comments", 0),
            "subreddit": post.get("subreddit", ""),
            "raw_data": {
                "title": title,
                "url": url,
                "selftext": post.get("selftext", ""),
                "author": post.get("author"),
                "score": score,
                "num_comments": post.get("num_comments", 0),
                "subreddit": post.get("subreddit", ""),
                "created_utc": created_utc,
            },
        }

    @property
    def config_min_score(self) -> int:
        return getattr(self, "_min_score", settings.reddit_min_score)

    @config_min_score.setter
    def config_min_score(self, value: int) -> None:
        self._min_score = value

    async def _fetch_subreddit(self, sub: str, config: RedditConfig) -> List[Dict[str, Any]]:
        posts = await self.client.get_subreddit_posts(sub, limit=config.limit_per_sub)
        normalized = []
        for post in posts:
            norm = self._normalize_post(post)
            if norm:
                normalized.append(norm)
        normalized.sort(key=lambda x: x.get("score", 0), reverse=True)
        return normalized

    async def fetch(self, config: RedditConfig = None) -> list[dict[str, Any]]:
        config = config or RedditConfig()
        self.config_min_score = config.min_score

        all_items: List[Dict[str, Any]] = []
        for sub in config.subreddits:
            try:
                items = await self._fetch_subreddit(sub, config)
                all_items.extend(items)
            except Exception as e:
                # 单子版块失败降级，不中断其他子版块
                logger.warning(f"Reddit r/{sub} 采集失败: {e}")
                continue
            await asyncio.sleep(1)

        # 时间过滤
        since = datetime.now(timezone.utc) - timedelta(hours=config.since_hours)
        all_items = [it for it in all_items if (it.get("published_at") or datetime.min.replace(tzinfo=timezone.utc)) >= since]

        # 跨源去重（Q10）
        all_items = self._deduper.deduplicate(all_items)
        all_items = all_items[:config.max_items]

        await self.save(all_items)
        logger.info(f"✅ Reddit 采集完成: {len(all_items)} 条")
        return all_items

    async def save(self, records: List[Dict[str, Any]]) -> None:
        """落库（复用共享 save_news_items）。"""
        for item in records:
            item.setdefault("metrics", {
                "category": item.get("category", "ai"),
                "published_at": item["published_at"].isoformat() if item.get("published_at") else None,
                "score": item.get("score"),
                "num_comments": item.get("num_comments"),
                "subreddit": item.get("subreddit"),
            })
        save_news_items(records, deduplicator=self._deduper)

    def configs(self):
        return [RedditConfig(
            min_score=settings.reddit_min_score,
            limit_per_sub=50,
            since_hours=72,
            max_items=settings.news_collection_limit,
        )]


async def main():
    collector = RedditCollector()
    items = await collector.fetch()
    print(f"Fetched: {len(items)} items")


if __name__ == "__main__":
    asyncio.run(main())
