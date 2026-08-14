import asyncio
import trafilatura
import httpx
from typing import Dict, Any, Optional
from readability import Document
from app.collectors.base import BaseAnalyzer
from app.collectors.github_analyzer import DataCleaner
from app.config import settings
from app.database.models import ContentItem
from app.logger import AppLogger

logger = AppLogger.get(__name__)


class NewsAnalyzer(BaseAnalyzer):
    NAME = "news"

    def __init__(self, client=None):
        self.cleaner = DataCleaner()
        self._http_client: httpx.AsyncClient | None = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
            )
        return self._http_client

    async def aclose(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    async def _fetch_article_content(self, url: str) -> str:
        """获取文章全文内容"""
        try:
            client = await self._get_http_client()
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

            # 尝试用 trafilatura 提取
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
            )
            if extracted and len(extracted) > 200:
                return extracted[:8000]

            # 备选：readability
            doc = Document(html)
            content = doc.summary()
            if content:
                # 简单清理 HTML
                import re
                content = re.sub(r'<[^>]+>', '', content)
                content = re.sub(r'\s+', ' ', content)
                return content[:8000]

            return ""
        except Exception as e:
            logger.warning(f"获取文章内容失败 {url}: {e}")
            return ""

    def _build_context_sections(self, item: ContentItem) -> Dict[str, str]:
        metrics = item.metrics or {}
        
        # 基本信息
        source_label = "Hacker News" if item.source == "hacker_news" else "新闻源"
        category_labels = {
            "ai": "AI/机器学习",
            "llm": "大语言模型",
            "research": "研究论文",
            "tools": "工具/框架",
            "industry": "行业动态",
        }
        category = category_labels.get(item.category, item.category or "AI")
        
        basic_info = (
            f"来源: {source_label} | "
            f"分类: {category} | "
            f"作者: {item.author or '未知'} | "
            f"链接: {item.url}\n"
        )
        
        if item.source == "hacker_news":
            hn_score = metrics.get("hn_score", 0)
            hn_comments = metrics.get("hn_comments", 0)
            basic_info += f"HN 分数: {hn_score} | 评论数: {hn_comments}\n"
        
        if metrics.get("feed_url"):
            basic_info += f"订阅源: {metrics['feed_url']}\n"
        
        if metrics.get("tags"):
            tags_str = ", ".join(metrics["tags"][:5])
            basic_info += f"标签: {tags_str}\n"

        # 发布时间
        published_at = metrics.get("published_at")
        if published_at:
            basic_info += f"发布时间: {published_at}\n"

        return {
            "basic_info": basic_info,
            "description": item.description or "无描述",
        }

    async def build_context(self, url: str) -> str:
        # 从 URL 查找 ContentItem
        db_item = await self._find_content_item(url)
        if not db_item:
            # 兜底：直接抓取 URL 内容
            content = await self._fetch_article_content(url)
            return self._build_fallback_prompt(url, content)

        context_sections = self._build_context_sections(db_item)

        # 获取文章全文：Q3 优先读采集时缓存的 raw_data['full_text']，
        # 仅在缓存缺失时才回退到联网抓取（避免漂移 / 减少网络依赖）。
        cached = ""
        if isinstance(db_item.raw_data, dict):
            cached = db_item.raw_data.get("full_text") or ""
        if cached:
            full_content = cached
        else:
            full_content = await self._fetch_article_content(url)
        if not full_content and db_item.description:
            full_content = db_item.description
        elif not full_content:
            full_content = db_item.raw_data.get("content", "") if db_item.raw_data else ""

        # 清理内容
        cleaned_content = self.cleaner.clean_readme(full_content, max_length=6000)

        prompt = f"""
你是一位专业的科技新闻分析师，请深度分析这篇新闻/文章，并用**纯 JSON** 格式返回结果，不要包含任何其他文字和 markdown 代码块。

文章标题：{db_item.title}
文章链接：{db_item.url}

## 基本信息
{context_sections['basic_info']}

## 摘要/描述
{context_sections['description']}

## 文章全文内容
{cleaned_content}

请结合以上**全部信息**，综合评估这篇内容对 AI/ML 从业者的价值，返回以下 JSON 结构（必须是有效的 JSON，不要包含任何其他内容）：
{{
  "one_liner": "一句话核心观点（15-25字）",
  "summary": "详细摘要（150-300字，包含关键事实、观点、影响）",
  "key_points": ["关键点1", "关键点2", "关键点3"],
  "tags": ["标签1", "标签2", "标签3"],
  "relevance_score": 0,
  "maturity_level": "新闻类型评估（突发新闻/深度分析/研究论文/产品发布/观点评论）",
  "source_quotes": ["原文摘录1（直接引用，不超过40字）", "原文摘录2（直接引用，不超过40字）"]
}}

**准确性硬约束（最重要）**：
- 只能基于上方提供的「文章全文内容」「摘要/描述」「基本信息」进行总结，**不得臆测、不得补充你已知但本文未提及的信息**。
- 若原文未提及某事实，summary 中不得出现该事实；宁可简短，不可编造。
- source_quotes 必须是**原文中真实存在的句子片段**（逐字摘录），不要用你改写后的话；如全文无法提供可靠摘录，返回空数组 []。

**评分标准（relevance_score 满分10分）**：
- 9.0+：重大突破、重要发布、深度技术分析，极高参考价值
- 7.5-8.9：有价值的技术分享、趋势分析、工具发布
- 6.0-7.4：一般性新闻、观点文章、入门级教程
- 低于6.0：营销内容、无关内容、质量较低

**maturity_level 取值**：
- "突发新闻" - 刚发生的重大事件
- "深度分析" - 长文分析、趋势报告
- "研究论文" - 学术论文、技术报告
- "产品发布" - 新模型、新工具、新版本发布
- "观点评论" - 个人观点、行业评论
"""
        return prompt

    async def _find_content_item(self, url: str) -> Optional[ContentItem]:
        """通过 URL 查找 ContentItem"""
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            return db.query(ContentItem).filter_by(url=url).first()
        finally:
            db.close()

    def _build_fallback_prompt(self, url: str, content: str) -> str:
        """兜底提示词"""
        cleaned = self.cleaner.clean_readme(content, max_length=6000)
        return f"""
你是一位专业的科技新闻分析师，请分析这篇文章。

文章链接：{url}

## 文章内容
{cleaned}

请返回纯 JSON：
{{
  "one_liner": "一句话核心观点（15-25字）",
  "summary": "详细摘要（150-300字）",
  "key_points": ["关键点1", "关键点2", "关键点3"],
  "tags": ["标签1", "标签2", "标签3"],
  "relevance_score": 0,
  "maturity_level": "新闻类型评估"
}}
"""


async def main():
    analyzer = NewsAnalyzer()
    # 测试用
    prompt = await analyzer.build_context("https://news.ycombinator.com/item?id=123456")
    print(prompt[:500])


if __name__ == "__main__":
    asyncio.run(main())