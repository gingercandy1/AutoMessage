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
    matches_keywords,
)
from app.config import settings
from app.logger import AppLogger

logger = AppLogger.get(__name__)

register_news_source("hf_papers")

PAPERS_API = "https://huggingface.co/api/daily_papers"


@dataclass
class HFPapersConfig:
    limit: int = 50
    min_upvotes: int = 0
    days_back: int = 1
    max_items: int = 100

    def __post_init__(self):
        if self.limit <= 0:
            self.limit = settings.hf_papers_limit


class HFPapersCollector(BaseCollector):
    NAME = "hf_papers"

    def __init__(self):
        self._deduper = NewsDeduplicator()
        self._keywords = [k.strip().lower() for k in settings.news_hn_keywords.split(",")]

    def _normalize_paper(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """标准化 HF Papers 条目为统一新闻 record。"""
        if not isinstance(entry, dict):
            return None

        paper = entry.get("paper") or entry
        if not isinstance(paper, dict):
            return None

        arxiv_id = paper.get("id")
        if not arxiv_id:
            return None

        title = (paper.get("title") or "").strip()
        if not title:
            return None

        # 复用 AI 关键词过滤（过滤 AI 之外的领域）
        summary = paper.get("summary") or ""
        text = f"{title} {summary}"
        if not matches_keywords(text, self._keywords):
            return None

        url = f"https://arxiv.org/abs/{arxiv_id}"
        published_at = None
        published = paper.get("publishedAt") or entry.get("publishedAt")
        if published:
            try:
                published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                published_at = None

        upvotes = paper.get("upvotes") or 0
        if upvotes < self._min_upvotes:
            return None

        authors = paper.get("authors") or []
        author_names = [a.get("name", "") for a in authors if isinstance(a, dict) and a.get("name")]
        author = ", ".join(author_names[:5]) or None

        github_repo = paper.get("githubRepo") or ""
        project_page = paper.get("projectPage") or ""

        return {
            "source": "hf_papers",
            "external_id": f"hf_paper_{arxiv_id}",
            "title": title,
            "url": url,
            "description": (summary or "")[:500],
            "author": author,
            "published_at": published_at,
            "category": "research",
            "upvotes": upvotes,
            "github_repo": github_repo,
            "project_page": project_page,
            "raw_data": {
                "arxiv_id": arxiv_id,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github_repo": github_repo,
                "project_page": project_page,
                "authors": author_names,
            },
        }

    async def _fetch_papers(self, config: HFPapersConfig) -> List[Dict[str, Any]]:
        """从 HF Papers API 拉取最近几天的每日论文。"""
        all_entries: List[Dict[str, Any]] = []
        today = datetime.now(timezone.utc)
        for day_offset in range(config.days_back):
            date_str = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    resp = await client.get(
                        PAPERS_API,
                        params={"date": date_str, "page": 1, "limit": config.limit},
                        headers={"User-Agent": "AIProjectBot/1.0 (hf-papers collector)"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                if isinstance(data, list):
                    entries = data
                elif isinstance(data, dict):
                    entries = data.get("results", [])
                else:
                    entries = []
                all_entries.extend(entries)
            except Exception as e:
                logger.warning(f"HF Papers 采集失败 ({date_str}): {e}")
                continue
        return all_entries

    async def fetch(self, config: HFPapersConfig = None) -> list[dict[str, Any]]:
        config = config or HFPapersConfig()
        self._min_upvotes = config.min_upvotes

        try:
            entries = await self._fetch_papers(config)
        except Exception as e:
            logger.warning(f"HF Papers 采集失败: {e}")
            return []
        items: List[Dict[str, Any]] = []
        for entry in entries:
            norm = self._normalize_paper(entry)
            if norm:
                items.append(norm)

        items = self._deduper.deduplicate(items)
        items = items[:config.max_items]

        await self.save(items)
        logger.info(f"✅ HF Papers 采集完成: {len(items)} 条")
        return items

    async def save(self, records: List[Dict[str, Any]]) -> None:
        """落库（复用共享 save_news_items）。"""
        for item in records:
            item.setdefault("metrics", {
                "category": "research",
                "published_at": item["published_at"].isoformat() if item.get("published_at") else None,
                "upvotes": item.get("upvotes"),
                "github_repo": item.get("github_repo"),
                "project_page": item.get("project_page"),
            })
        save_news_items(records, deduplicator=self._deduper)

    def configs(self):
        return [HFPapersConfig(
            limit=settings.hf_papers_limit,
            min_upvotes=0,
            days_back=1,
            max_items=settings.news_collection_limit,
        )]


async def main():
    collector = HFPapersCollector()
    items = await collector.fetch()
    print(f"Fetched: {len(items)} items")


if __name__ == "__main__":
    asyncio.run(main())
