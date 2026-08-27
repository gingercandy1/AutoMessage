import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

from app.collectors.base import BaseCollector
from app.collectors.news.news_base import (
    NewsDeduplicator,
    save_news_items,
    register_news_source,
    categorize_news,
)
from app.config import settings
from app.logger import AppLogger

logger = AppLogger.get(__name__)

register_news_source("producthunt")

GRAPHQL_ENDPOINT = "https://api.producthunt.com/v2/api/graphql"

# 按最新发布排序（Older to newest 里的 newest 方向由 order 控制）
GET_POSTS_QUERY = """
query GetPosts($first: Int!) {
  posts(order: NEWEST, first: $first) {
    edges {
      node {
        id
        name
        tagline
        url
        votesCount
        commentsCount
        createdAt
        description
        thumbnail {
          url
        }
      }
    }
  }
}
"""


@dataclass
class ProductHuntConfig:
    min_votes: int = 20
    since_hours: int = 48
    limit: int = 50
    max_items: int = 100

    def __post_init__(self):
        if self.min_votes <= 0:
            self.min_votes = settings.product_hunt_min_votes


class ProductHuntCollector(BaseCollector):
    NAME = "producthunt"

    def __init__(self):
        self._deduper = NewsDeduplicator()
        self.token = settings.product_hunt_token

    @property
    def enabled(self) -> bool:
        """无 token 时跳过该源（T03）。"""
        return bool(self.token and self.token.strip())

    def _normalize_post(self, node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """标准化 Product Hunt 产品为统一新闻 record。"""
        if not isinstance(node, dict):
            return None

        name = (node.get("name") or "").strip()
        if not name:
            return None

        tagline = node.get("tagline") or ""
        votes = node.get("votesCount") or 0
        if votes < self._min_votes:
            return None

        url = node.get("url") or ""
        if not url:
            return None

        published_at = None
        created_at = node.get("createdAt")
        if created_at:
            try:
                published_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                published_at = None

        external_id = f"ph_{node.get('id')}"
        image_url = (node.get("thumbnail") or {}).get("url")
        return {
            "source": "producthunt",
            "external_id": external_id,
            "title": name,
            "url": url,
            "description": (tagline or (node.get("description") or ""))[:500],
            "author": None,
            "published_at": published_at,
            "category": categorize_news(name, tagline, "producthunt"),
            "votes": votes,
            "comments_count": node.get("commentsCount") or 0,
            "raw_data": {
                "name": name,
                "tagline": tagline,
                "url": url,
                "votesCount": votes,
                "commentsCount": node.get("commentsCount", 0),
                "id": node.get("id"),
                "image_url": image_url,
            },
        }

    async def _fetch_posts(self, config: ProductHuntConfig) -> List[Dict[str, Any]]:
        if not self.enabled:
            logger.warning("⚠️ Product Hunt token 未配置，跳过该源（T03）。")
            return []

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.post(
                GRAPHQL_ENDPOINT,
                json={"query": GET_POSTS_QUERY, "variables": {"first": config.limit}},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        edges = data.get("data", {}).get("posts", {}).get("edges", [])
        posts = []
        for edge in edges:
            norm = self._normalize_post(edge.get("node"))
            if norm:
                posts.append(norm)

        # 时间过滤
        since = datetime.now(timezone.utc) - timedelta(hours=config.since_hours)
        posts = [
            p for p in posts
            if (p.get("published_at") or datetime.min.replace(tzinfo=timezone.utc)) >= since
        ]
        posts.sort(key=lambda x: x.get("votes", 0), reverse=True)
        return posts

    async def fetch(self, config: ProductHuntConfig = None) -> int:
        config = config or ProductHuntConfig()
        self._min_votes = config.min_votes

        try:
            posts = await self._fetch_posts(config)
        except Exception as e:
            # 源级失败降级，不阻塞其他源
            logger.warning(f"⚠️ Product Hunt 采集失败: {e}")
            return 0

        posts = self._deduper.deduplicate(posts)
        posts = posts[:config.max_items]

        await self.save(posts)
        logger.info(f"✅ Product Hunt 采集完成: {len(posts)} 条")
        return len(posts)

    async def save(self, records: List[Dict[str, Any]]) -> None:
        """落库（复用共享 save_news_items）。"""
        for item in records:
            item.setdefault("metrics", {
                "category": item.get("category", "ai"),
                "published_at": item["published_at"].isoformat() if item.get("published_at") else None,
                "votes": item.get("votes"),
                "comments_count": item.get("comments_count"),
            })
        save_news_items(records, deduplicator=self._deduper)

    def configs(self):
        return [ProductHuntConfig(
            min_votes=settings.product_hunt_min_votes,
            since_hours=48,
            limit=50,
            max_items=settings.news_collection_limit,
        )]


async def main():
    collector = ProductHuntCollector()
    items = await collector.fetch()
    print(f"Fetched: {len(items)} items")


if __name__ == "__main__":
    asyncio.run(main())
