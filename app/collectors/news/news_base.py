from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
import trafilatura
from rapidfuzz import fuzz
from sqlalchemy.dialects.postgresql import insert, JSONB
from sqlalchemy import or_, cast

from app.config import settings
from app.database import SessionLocal
from app.database.models import ContentItem, MetricSnapshot, NewsCluster
from app.logger import AppLogger

logger = AppLogger.get(__name__)


async def fetch_full_text(url: str) -> str:
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


#: 所有属于“新闻”的 ContentItem.source 值。显式列出全部新闻族源，
#: 消除对采集器模块导入顺序的依赖（register_news_source 保留，幂等兼容）。
NEWS_SOURCES: List[str] = [
    "hacker_news",
    "news",
    "hf_papers",
    "official_blog",
    "producthunt",
]


def register_news_source(source: str) -> None:
    """把新源登记为新闻源（幂等）。"""
    if source and source not in NEWS_SOURCES:
        NEWS_SOURCES.append(source)
        logger.info(f"📰 已登记新闻源: {source}")


def is_news_source(source: str) -> bool:
    return source in NEWS_SOURCES


#: AI 相关关键词（小写，支持词组匹配）
AI_KEYWORDS = [
    "llm", "large language model", "gpt", "claude", "llama", "mistral", "gemini",
    "deepseek", "fine-tun", "instruct", "chatgpt", "agent", "transformer", "neural",
    "deep learning", "machine learning", "artificial intelligence", "generative",
    "ai", "ml", "multimodal", "openai", "anthropic",
]


def matches_keywords(text: str, keywords: List[str] = None) -> bool:
    """检查文本是否包含关键词。keywords 缺省时用 AI_KEYWORDS。"""
    text_lower = (text or "").lower()
    for kw in (keywords or AI_KEYWORDS):
        kw_lower = kw.lower()
        if kw_lower in text_lower:
            return True
        if " " in kw_lower:
            parts = kw_lower.split()
            if all(part in text_lower for part in parts):
                return True
    return False


def categorize_news(title: str, description: str = "", source: str = "news") -> str:
    """根据标题和描述对新闻进行分类。"""
    text = f"{title} {description}".lower()

    # LLM/大模型相关 - 放在最前面，优先匹配
    if any(kw in text for kw in [
        "llm", "large language model", "gpt", "claude", "llama", "mistral", "gemini",
        "fine-tun", "instruct", "chat", "大模型", "大语言模型", "微调",
    ]):
        return "llm"

    # 研究论文相关
    if any(kw in text for kw in [
        "arxiv", "paper", "research", "study", "benchmark", "evaluation",
        "论文", "研究", "评测",
    ]):
        return "research"

    # AI 通用
    if any(kw in text for kw in [
        "ai", "artificial intelligence", "machine learning", "deep learning", "neural",
        "transformer", "generative", "人工智能", "机器学习", "深度学习", "神经网络",
    ]):
        return "ai"

    # 工具/框架
    if any(kw in text for kw in [
        "tool", "framework", "library", "sdk", "api", "github", "open source", "release", "launch",
        "工具", "框架", "开源", "发布", "上线",
    ]):
        return "tools"

    # 行业/商业
    if any(kw in text for kw in [
        "funding", "investment", "acquisition", "startup", "company", "enterprise", "business", "market",
        "融资", "收购", "创业", "公司", "企业", "商业",
    ]):
        return "industry"

    return "ai"  # 默认分类


class NewsDeduplicator:
    """跨源新闻去重 + 聚类（从 NewsCollector 抽取，供所有新闻采集器复用）。"""

    def __init__(self, enabled: Optional[bool] = None):
        self.enabled = settings.news_dedup_enabled if enabled is None else enabled
        self._last_clusters: List[List[int]] = []

    def levenshtein_ratio(self, a, b) -> float:
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        return fuzz.ratio(a, b, score_cutoff=85.0) / 100.0

    def normalize_url(self, url: str) -> str:
        """归一化 URL：去 scheme、尾部斜杠、www.、查询参数排序，用于精确去重。"""
        if not url:
            return ""
        u = url.strip().lower()
        u = u.replace("https://", "").replace("http://", "").replace("www.", "")
        u = u.split("?")[0].split("#")[0]
        if u.endswith("/"):
            u = u[:-1]
        return u

    def deduplicate(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """去重 + 聚类（Q10）。返回保留项，并记录跨源簇到 self._last_clusters。"""
        if not self.enabled or not items:
            self._last_clusters = []
            return items

        clusters: List[List[int]] = []
        url_to_cluster: Dict[str, int] = {}
        title_to_cluster: List[tuple] = []

        unique: List[Dict[str, Any]] = []
        for idx, item in enumerate(items):
            url = self.normalize_url(item.get("url", ""))
            title = (item.get("title", "") or "").strip().lower()

            if url and url in url_to_cluster:
                clusters[url_to_cluster[url]].append(idx)
                continue
            merged = False
            for t_norm, c_idx in title_to_cluster:
                if self.levenshtein_ratio(title, t_norm) >= 0.85:
                    clusters[c_idx].append(idx)
                    merged = True
                    break
            if merged:
                continue

            new_c = len(clusters)
            clusters.append([idx])
            unique.append(item)
            if url:
                url_to_cluster[url] = new_c
            if title:
                title_to_cluster.append((title, new_c))

        self._last_clusters = [grp for grp in clusters if len(grp) > 1]
        logger.info(f"🔄 去重+聚类: {len(items)} -> {len(unique)} (簇:{len(self._last_clusters)})")
        return unique

    def write_clusters(self, item_id_by_index: Dict[int, int], db=None) -> None:
        if not self._last_clusters:
            return

        owns_session = db is None
        db = db or SessionLocal()
        try:
            for grp in self._last_clusters:
                member_ids = [item_id_by_index[i] for i in grp if i in item_id_by_index]
                if len(member_ids) < 2:
                    continue

                conditions = [
                    NewsCluster.member_item_ids.op("@>")(cast([mid], JSONB))
                    for mid in member_ids
                ]
                existing = db.query(NewsCluster).filter(or_(*conditions)).first()

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

            if owns_session:
                db.commit()
        except Exception:
            if owns_session:
                db.rollback()
            raise
        finally:
            if owns_session:
                db.close()


def save_news_items(
    records: List[Dict[str, Any]],
    deduplicator: Optional[NewsDeduplicator] = None,
) -> None:
    """共享落库：ContentItem upsert + MetricSnapshot + 新闻簇写入。"""
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    today = now.date()
    item_id_by_index: Dict[int, int] = {}

    try:
        for index, item in enumerate(records):
            source = item["source"]
            external_id = item["external_id"]
            metrics = item.get("metrics") or {}

            existing = db.query(ContentItem).filter_by(
                source=source, external_id=external_id
            ).first()

            if existing:
                existing.title = item["title"]
                existing.url = item["url"]
                existing.description = item["description"]
                existing.author = item.get("author")
                existing.category = item.get("category")
                existing.metrics = metrics
                existing.raw_data = item.get("raw_data")
                content_item = existing
            else:
                # content_items.url 有独立的唯一约束，不分 source：同一 URL 若已被
                # 别的源（或本源更早的 external_id 方案）先插入，这里直接插入会在
                # db.flush() 抛 IntegrityError，导致本函数一次没 commit 的整批全部
                # 回滚（见 news_collector.py 的同类修复注释）。这里同样先按 url
                # 查一次兜底，撞了就复用已有行而不重复插入。
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
                        metrics=metrics,
                        raw_data=item.get("raw_data"),
                    )
                    db.add(content_item)
                    db.flush()

            item_id_by_index[index] = content_item.id

            # MetricSnapshot
            stmt = insert(MetricSnapshot).values(
                content_item_id=content_item.id,
                source=source,
                external_id=external_id,
                category=item.get("category", "ai"),
                rank=None,
                metrics=metrics,
                captured_date=today,
                captured_at=now,
            ).on_conflict_do_update(
                index_elements=["source", "external_id", "captured_date"],
                set_={
                    "content_item_id": content_item.id,
                    "rank": None,
                    "metrics": metrics,
                    "captured_at": now,
                }
            )
            db.execute(stmt)

        if deduplicator:
            deduplicator.write_clusters(item_id_by_index, db)

        db.commit()
        logger.info(f"✅ 新闻入库完成: {len(records)} 条")

    except Exception as e:
        # 不 rollback 直接 close 会把 session 留在 aborted 事务状态，且此前用
        # print() 输出到 stdout，不进 AppLogger，批量丢失完全无感知。
        db.rollback()
        logger.error(f"❌ 新闻入库失败，本批 {len(records)} 条整体回滚: {e}")
    finally:
        db.close()
