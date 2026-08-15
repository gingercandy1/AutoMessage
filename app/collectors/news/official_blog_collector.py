"""官方模型发布博客采集器（T04）。

统一爬取 5 家官方发布页（OpenAI/Anthropic/DeepSeek/Mistral/Google AI），
用 trafilatura 从列表页提取最新文章链接，再抓取全文缓存到 raw_data['full_text']。
单厂商页面失败降级，不中断其他厂商。
"""
import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from app.collectors.base import BaseCollector
from app.collectors.news.news_base import (
    NewsDeduplicator,
    save_news_items,
    register_news_source,
    categorize_news,
    fetch_full_text,
)
from app.config import settings
from app.logger import AppLogger

logger = AppLogger.get(__name__)

register_news_source("official_blog")

# 列表页里常出现的导航噪音 URL 片段，过滤掉避免收录分类页/标签页
_BAD_PATH_FRAGMENTS = (
    "/category/", "/tag/", "/author/", "/about", "/contact", "/privacy",
    "/terms", "/careers", "/page/", "/?", "javascript:", "/rss", "/feed",
)


@dataclass
class OfficialBlogConfig:
    feed_urls: Optional[List[str]] = None
    max_per_vendor: int = 5
    max_items: int = 50
    since_hours: int = 72
    user_agent: str = "AIProjectBot/1.0 (official-blog collector)"

    def __post_init__(self):
        if self.feed_urls is None:
            self.feed_urls = [
                f.strip() for f in settings.official_blog_feeds.split(",") if f.strip()
            ]


class OfficialBlogCollector(BaseCollector):
    NAME = "official_blog"

    def __init__(self):
        self._deduper = NewsDeduplicator()

    async def _fetch_listing(self, feed_url: str, config: OfficialBlogConfig) -> List[str]:
        """从列表页提取文章 URL 列表。失败返回 []。"""
        try:
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=True,
                headers={"User-Agent": config.user_agent},
            ) as client:
                resp = await client.get(feed_url)
                resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.warning(f"官方博客列表页抓取失败 {feed_url}: {e}")
            return []

        # 用正则提取候选链接（相对路径转绝对），保留 URL 结构
        candidate_links: List[str] = []
        href_pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)
        for href in href_pattern.findall(html):
            absolute = urljoin(feed_url, href)
            if absolute.startswith(("mailto:", "tel:", "javascript:")):
                continue
            candidate_links.append(absolute)

        seen: set = set()
        article_urls: List[str] = []
        for href in candidate_links:
            # 去锚点/查询，过滤噪音路径
            clean = href.split("#")[0]
            if not clean or clean.lower() in _BAD_PATH_FRAGMENTS:
                continue
            if any(frag in clean for frag in _BAD_PATH_FRAGMENTS):
                continue
            if clean in seen:
                continue
            seen.add(clean)
            article_urls.append(clean)
            if len(article_urls) >= config.max_per_vendor:
                break

        logger.info(f"✅ 官方博客 {feed_url}: 提取 {len(article_urls)} 篇")
        return article_urls

    async def _fetch_article(self, url: str) -> Optional[Dict[str, Any]]:
        """抓取单篇文章标题 + 全文缓存。"""
        full_text = await fetch_full_text(url)
        if not full_text:
            return None

        title = full_text.splitlines()[0].strip() if full_text else ""
        if not title or len(title) > 200:
            title = "Official blog update"

        return {
            "source": "official_blog",
            "external_id": f"blog_{hashlib.md5(url.encode()).hexdigest()[:16]}",
            "title": title,
            "url": url,
            "description": full_text[:500],
            "author": None,
            "published_at": None,
            "category": categorize_news(title, full_text[:500], "official_blog"),
            "raw_data": {"url": url, "full_text": full_text[:8000]},
        }

    async def fetch(self, config: OfficialBlogConfig = None) -> list[dict[str, Any]]:
        config = config or OfficialBlogConfig()

        all_items: List[Dict[str, Any]] = []
        for feed_url in config.feed_urls:
            try:
                urls = await self._fetch_listing(feed_url, config)
                for url in urls:
                    article = await self._fetch_article(url)
                    if article:
                        all_items.append(article)
                    await asyncio.sleep(0.5)
            except Exception as e:
                # 单厂商失败降级，不中断其他厂商
                logger.warning(f"官方博客 {feed_url} 采集失败: {e}")
                continue

        all_items = self._deduper.deduplicate(all_items)
        all_items = all_items[:config.max_items]

        await self.save(all_items)
        logger.info(f"✅ 官方博客采集完成: {len(all_items)} 条")
        return all_items

    async def save(self, records: List[Dict[str, Any]]) -> None:
        """落库（复用共享 save_news_items）。"""
        for item in records:
            item.setdefault("metrics", {
                "category": item.get("category", "ai"),
                "published_at": None,
                "vendor": self._vendor_of(item["url"]),
            })
        save_news_items(records, deduplicator=self._deduper)

    @staticmethod
    def _vendor_of(url: str) -> str:
        m = re.search(r"https?://([^/]+)", url)
        if m:
            host = m.group(1).replace("www.", "")
            return host.split(".")[0]
        return "unknown"

    def configs(self):
        return [OfficialBlogConfig(
            max_per_vendor=5,
            max_items=settings.news_collection_limit,
            since_hours=72,
        )]


async def main():
    collector = OfficialBlogCollector()
    items = await collector.fetch()
    print(f"Fetched: {len(items)} items")


if __name__ == "__main__":
    asyncio.run(main())
