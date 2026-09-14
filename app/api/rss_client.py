import asyncio
import hashlib
import re
import httpx
import feedparser
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from app.logger import AppLogger
from app.config import settings

logger = AppLogger.get(__name__)


@dataclass
class RSSFeedEntry:
    """RSS 订阅条目"""
    title: str
    url: str
    description: str
    author: str | None
    published_at: datetime | None
    feed_url: str
    content: str | None = None
    tags: List[str] | None = None
    image_url: str | None = None

    @property
    def content_hash(self) -> str:
        """生成内容哈希用于去重"""
        content = f"{self.title}{self.url}{self.description}"
        return hashlib.md5(content.encode()).hexdigest()


class RSSClient:
    """通用 RSS/Atom 订阅获取器"""

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str = "AIProjectBot/1.0 (+https://github.com/ai-project-bot)",
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "RSSClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _fetch_feed(self, feed_url: str) -> Optional[str]:
        """获取 RSS 源内容"""
        client = await self._get_client()
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await client.get(feed_url)
                resp.raise_for_status()
                return resp.text
            except httpx.HTTPStatusError as e:
                last_exc = e
                status = e.response.status_code
                if status == 429:
                    wait = 2 ** attempt
                    logger.warning(
                        f"RSS 速率限制，{wait}s 后重试 ({attempt}/{self.max_retries}): {feed_url}"
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"RSS HTTP 错误 {status}: {feed_url}")
                return None
            except httpx.RequestError as e:
                last_exc = e
                wait = 2 ** attempt
                logger.warning(
                    f"RSS 请求失败，{wait}s 后重试 ({attempt}/{self.max_retries}): {e}"
                )
                await asyncio.sleep(wait)

        logger.error(f"RSS 多次重试后仍失败: {feed_url}")
        return None

    def _parse_entry(self, entry: feedparser.FeedParserDict, feed_url: str) -> Optional[RSSFeedEntry]:
        """解析单个 RSS 条目"""
        try:
            title = entry.get("title", "").strip()
            if not title:
                return None

            url = entry.get("link", "").strip()
            if not url:
                return None

            description = entry.get("summary", "") or entry.get("description", "") or ""
            description = description.strip()

            author = entry.get("author", "") or ""
            author = author.strip() if author else None

            published_at = None
            for date_field in ["published_parsed", "updated_parsed", "created_parsed"]:
                if entry.get(date_field):
                    try:
                        dt_struct = entry[date_field]
                        published_at = datetime(
                            dt_struct.tm_year, dt_struct.tm_mon, dt_struct.tm_mday,
                            dt_struct.tm_hour, dt_struct.tm_min, dt_struct.tm_sec,
                            tzinfo=timezone.utc
                        )
                        break
                    except Exception:
                        continue

            content = None
            if entry.get("content"):
                content = entry["content"][0].get("value", "")
            elif entry.get("description"):
                content = entry["description"]

            tags = []
            if entry.get("tags"):
                tags = [tag.get("term", "") for tag in entry["tags"] if tag.get("term")]

            image_url = self._extract_image_url(entry, content or description)

            return RSSFeedEntry(
                title=title,
                url=url,
                description=description,
                author=author,
                published_at=published_at,
                feed_url=feed_url,
                content=content,
                tags=tags,
                image_url=image_url,
            )
        except Exception as e:
            logger.warning(f"解析 RSS 条目失败: {e}")
            return None

    @staticmethod
    def _extract_image_url(entry: feedparser.FeedParserDict, html_fallback: str | None) -> str | None:
        """从 RSS 条目提取配图：media:content > media:thumbnail > enclosure(image/*) >
        content/description 里第一个 <img src>。找不到就 None（纯文字卡）。"""
        for media_field in ("media_content", "media_thumbnail"):
            media = entry.get(media_field)
            if media:
                url = media[0].get("url")
                if url:
                    return url

        for enc in entry.get("enclosures", []) or []:
            enc_type = enc.get("type", "")
            href = enc.get("href") or enc.get("url")
            if href and enc_type.startswith("image/"):
                return href

        if html_fallback:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_fallback)
            if m:
                return m.group(1)

        return None

    async def fetch_feed(
        self,
        feed_url: str,
        since: Optional[datetime] = None,
        max_entries: int = 50,
    ) -> List[RSSFeedEntry]:
        """获取并解析单个 RSS 源"""
        logger.info(f"🔍 抓取 RSS: {feed_url}")
        
        xml_content = await self._fetch_feed(feed_url)
        if not xml_content:
            return []

        feed = feedparser.parse(xml_content)
        if feed.bozo and feed.bozo_exception:
            logger.warning(f"RSS 解析警告 ({feed_url}): {feed.bozo_exception}")

        entries = []
        for entry in feed.entries[:max_entries]:
            parsed = self._parse_entry(entry, feed_url)
            if parsed:
                if since and parsed.published_at and parsed.published_at < since:
                    continue
                entries.append(parsed)

        logger.info(f"✅ RSS {feed_url} 解析完成: {len(entries)} 条")
        return entries

    async def fetch_multiple_feeds(
        self,
        feed_urls: List[str],
        since: Optional[datetime] = None,
        max_entries_per_feed: int = 50,
        max_concurrent: int = 5,
    ) -> List[RSSFeedEntry]:
        """并发获取多个 RSS 源"""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _fetch_one(url: str):
            async with semaphore:
                try:
                    return await self.fetch_feed(url, since, max_entries_per_feed)
                except Exception as e:
                    logger.warning(f"获取 RSS {url} 失败: {e}")
                    return []

        tasks = [_fetch_one(url) for url in feed_urls]
        results = await asyncio.gather(*tasks)
        
        all_entries = []
        for entries in results:
            all_entries.extend(entries)
        
        return all_entries


async def main():
    client = RSSClient()
    feeds = settings.news_rss_feeds.split(",")
    feeds = [f.strip() for f in feeds if f.strip()]
    
    since = datetime.now(timezone.utc) - timedelta(days=1)
    entries = await client.fetch_multiple_feeds(feeds[:3], since=since)
    
    for entry in entries[:5]:
        print(f"Title: {entry.title}")
        print(f"URL: {entry.url}")
        print(f"Published: {entry.published_at}")
        print(f"Feed: {entry.feed_url}")
        print("---")


if __name__ == "__main__":
    asyncio.run(main())