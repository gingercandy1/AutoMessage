import asyncio
import hashlib
from rapidfuzz import fuzz
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Set

import httpx
import trafilatura
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

    def _levenshtein_ratio(self, a, b):
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        return fuzz.ratio(a, b, score_cutoff=85.0) / 100.0

    def _normalize_url(self, url: str) -> str:
        """归一化 URL：去 scheme、尾部斜杠、www.、查询参数排序，用于精确去重。"""
        if not url:
            return ""
        u = url.strip().lower()
        u = u.replace("https://", "").replace("http://", "").replace("www.", "")
        u = u.split("?")[0].split("#")[0]
        if u.endswith("/"):
            u = u[:-1]
        return u

    def _deduplicate(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """去重 + 聚类（Q10）。

        两阶段：
          1) exact_url：归一化 URL 完全相同 -> 同一簇
          2) title_similarity：标题 Levenshtein 相似度 >= 0.85 -> 同一簇
        返回保留项（每簇第一条为 canonical），并把簇信息暂存到 self._last_clusters。
        """
        if not settings.news_dedup_enabled or not items:
            self._last_clusters = []
            return items

        clusters: List[List[int]] = []          # 每个簇是 items 下标列表
        url_to_cluster: Dict[str, int] = {}
        title_to_cluster: List[tuple] = []       # (norm_title, cluster_idx)

        unique: List[Dict[str, Any]] = []
        for idx, item in enumerate(items):
            url = self._normalize_url(item.get("url", ""))
            title = (item.get("title", "") or "").strip().lower()

            # 1) exact url
            if url and url in url_to_cluster:
                clusters[url_to_cluster[url]].append(idx)
                continue
            # 2) title similarity
            merged = False
            for t_norm, c_idx in title_to_cluster:
                if self._levenshtein_ratio(title, t_norm) >= 0.85:
                    clusters[c_idx].append(idx)
                    merged = True
                    break
            if merged:
                continue

            # 新簇
            new_c = len(clusters)
            clusters.append([idx])
            unique.append(item)
            if url:
                url_to_cluster[url] = new_c
            if title:
                title_to_cluster.append((title, new_c))

        # 只在簇大小>1 时记录（用于写入 NewsCluster）
        self._last_clusters = [grp for grp in clusters if len(grp) > 1]
        logger.info(f"🔄 去重+聚类: {len(items)} -> {len(unique)} (簇:{len(self._last_clusters)})")
        return unique

    def _write_clusters(self, item_id_by_index: Dict[int, int]):
        """将本次批次内的簇写入 NewsCluster 表（canonical=簇首）。

        跨运行去重（Q10 v1）：若某个成员已属于既有 NewsCluster，则把本次新成员
        并入既有簇（以既有 canonical 为准），不再新建重复 canonical，避免同一
        新闻在多次采集后产生多个簇、进而导致频道重复推送。
        """
        if not getattr(self, "_last_clusters", None):
            return
        db = SessionLocal()
        try:
            for grp in self._last_clusters:
                member_ids = [item_id_by_index[i] for i in grp if i in item_id_by_index]
                if len(member_ids) < 2:
                    continue
                # 既有簇查找：任一成员已出现在某个 NewsCluster 里
                existing = (
                    db.query(NewsCluster)
                    .filter(NewsCluster.member_item_ids.op("&&")(member_ids))
                    .first()
                )
                if existing:
                    merged = list(set((existing.member_item_ids or []) + member_ids))
                    existing.member_item_ids = merged
                    db.add(existing)
                    continue
                db.add(NewsCluster(
                    canonical_item_id=member_ids[0],
                    member_item_ids=member_ids,
                    cluster_method="exact_url|title_similarity",
                ))
            db.commit()
        finally:
            db.close()

    async def _fetch_full_text(self, url: str) -> str:
        """抓取并提取文章正文（Q3：采集时缓存全文，避免摘要时再联网漂移）。

        失败返回空串，绝不抛出——采集流程不能因单篇正文抓取失败而中断。
        """
        try:
            async with httpx.AsyncClient(
                timeout=20.0, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            extracted = trafilatura.extract(
                resp.text, include_comments=False, include_tables=False, no_fallback=False
            )
            if extracted and len(extracted) > 200:
                return extracted[:8000]
            return ""
        except Exception as e:
            logger.warning(f"新闻正文抓取失败 {url}: {e}")
            return ""

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

    async def fetch(self, config: NewsConfig) -> list[dict[str, Any]]:
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
        return all_items

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