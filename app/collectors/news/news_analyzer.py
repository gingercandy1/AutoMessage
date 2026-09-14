import asyncio
import trafilatura
import httpx
from typing import Dict, Optional
from readability import Document
from app.collectors.base import BaseAnalyzer
from app.collectors.github_analyzer import DataCleaner
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

        if db_item.source == "hf_papers":
            return self._build_hf_paper_prompt(db_item, context_sections, cleaned_content)

        prompt = f"""
你是一位严谨的科技新闻编辑，任务是为一篇新闻报道撰写**新闻梗概（news brief）**，并用**纯 JSON** 格式返回结果，不要包含任何其他文字和 markdown 代码块。

文章标题：{db_item.title}
文章链接：{db_item.url}

## 基本信息
{context_sections['basic_info']}

## 摘要/描述
{context_sections['description']}

## 文章全文内容
{cleaned_content}

**写作要求（新闻梗概，不是评论分析）**：
- 你的产出是一则客观的消息梗概：转述「这篇报道说了什么」，而不是「你怎么看这件事」。
- 覆盖新闻要素：谁/哪家机构、发生了什么、何时、关键数字与数据、文中给出的背景或影响。
- 使用中性、陈述性的新闻语言；**禁用**评价性和推断性措辞（如"令人振奋""重大突破""这意味着未来将…"），除非那是在明确转述原文中的观点，此时须注明出处（如"报道称""该公司表示"）。
- 人名、机构名、产品名、数字必须与原文逐字一致；原文未提及的一律不写。
- 不做预测、不建议、不补充你的外部知识；宁可简短，不可编造。

请返回以下 JSON 结构（必须是有效的 JSON，不要包含任何其他内容）：
{{
  "one_liner": "一句话核心事实（15-25字，陈述句）",
  "summary": "新闻梗概（150-300字，按新闻要素组织：主体—事件—时间—关键数据—文内所述影响；不含评论）",
  "key_points": ["关键事实1", "关键事实2", "关键事实3"],
  "tags": ["标签1", "标签2", "标签3"],
  "relevance_score": 0,
  "maturity_level": "新闻类型评估（突发新闻/深度分析/研究论文/产品发布/观点评论）",
  "source_quotes": ["原文摘录1（直接引用，不超过40字）", "原文摘录2（直接引用，不超过40字）"]
}}

**准确性硬约束（最重要）**：
- 只能基于上方提供的「文章全文内容」「摘要/描述」「基本信息」进行总结，**不得臆测、不得补充你已知但本文未提及的信息**。
- 若原文未提及某事实，summary 中不得出现该事实；宁可简短，不可编造。
- 区分「事实」与「主张」：原文中各方的说法须保留归属（谁说的），不得写成已证实的事实。
- source_quotes 必须是**原文中真实存在的句子片段**(逐字摘录)，不要用你改写后的话；如全文无法提供可靠摘录，返回空数组 []。

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

    def _build_hf_paper_prompt(self, db_item: ContentItem, context_sections: Dict[str, str], cleaned_content: str) -> str:
        """HF Papers 专用 prompt：在新闻梗概之外，额外要求 LLM 给出「实用性/
        应用性」评分。采集阶段（hf_papers_collector）只用点赞数/是否带代码仓库
        做粗筛，挡不住"有代码但本质是纯理论证明"这类论文；这里让 LLM 读完整摘要
        后判断"能不能被工程师直接拿去用/复现"，分数写进 Summary.score，供
        processor.py 据此自动 reject 学术性过强的论文（不进审查/推送队列）。
        """
        raw = db_item.raw_data or {}
        github_repo = raw.get("github_repo") or ""
        project_page = raw.get("project_page") or ""
        resource_line = (
            f"GitHub 仓库: {github_repo or '无'} | 项目主页: {project_page or '无'}"
        )

        return f"""
你是一位关注技术落地的 AI 研究编辑，任务是为一篇 HuggingFace Daily Papers 收录的论文撰写摘要，并**重点评估它的实用性/应用性**——即一名工程师或从业者能否基于这篇论文直接用起来，而不是只有理论价值。用**纯 JSON** 格式返回结果，不要包含任何其他文字和 markdown 代码块。

论文标题：{db_item.title}
论文链接：{db_item.url}

## 基本信息
{context_sections['basic_info']}
{resource_line}

## 摘要/描述
{context_sections['description']}

## 论文全文/摘要内容
{cleaned_content}

**写作与评分要求**：
- summary 客观转述论文做了什么、解决什么问题、关键方法与结果，不做营销式吹捧。
- 人名、机构名、方法名、数字必须与原文逐字一致；原文未提及的一律不写。
- score 是**实用性/应用性评分**，不是"论文质量"或"学术贡献"评分：
  - 是否有可运行的代码/权重/项目页（有 GitHub 仓库/项目主页显著加分）
  - 是否解决具体的工程问题、能否被复现或直接调用
  - 是否只是数学证明、理论分析、纯 benchmark 报告、无代码无法复现（应显著扣分）
- 宁可保守打低分，不要因为论文"有意思"就打高分；纯理论/太专业、没有落地路径的论文应给低分（3分以下）。

请返回以下 JSON 结构（必须是有效的 JSON，不要包含任何其他内容）：
{{
  "one_liner": "一句话核心结论（15-25字，陈述句）",
  "summary": "论文摘要（150-300字：解决什么问题、核心方法、关键结果）",
  "key_points": ["关键事实1", "关键事实2", "关键事实3"],
  "tags": ["标签1", "标签2", "标签3"],
  "score": 0,
  "maturity_level": "研究论文",
  "source_quotes": ["原文摘录1（直接引用，不超过40字）", "原文摘录2（直接引用，不超过40字）"]
}}

**准确性硬约束（最重要）**：
- 只能基于上方提供的内容总结，不得臆测、不得补充你已知但本文未提及的信息。
- source_quotes 必须是原文中真实存在的句子片段（逐字摘录）；若无法提供可靠摘录，返回空数组 []。

**score 评分标准（实用性/应用性，满分10分）**：
- 8.5+：有开源代码/权重、可直接复现或调用，解决明确的工程/产品问题
- 6.0-8.4：有一定应用潜力，但需要较多工程改造或数据才能落地
- 3.0-5.9：偏理论，应用路径不清晰，或只是小范围 benchmark 提升
- 3.0 以下：纯数学证明/理论分析/无代码无法验证，学术性远大于实用性
"""

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
你是一位严谨的科技新闻编辑，任务是为这篇报道撰写**新闻梗概（news brief）**：客观转述「报道说了什么」，不做评论分析。

写作要求：
- 覆盖新闻要素：主体—事件—时间—关键数据—文内所述影响。
- 中性陈述语言；禁用评价性、推断性措辞；原文中的说法须注明出处。
- 人名、机构名、数字与原文逐字一致；原文未提及的一律不写，不得补充外部知识。

文章链接：{url}

## 文章内容
{cleaned}

请返回纯 JSON：
{{
  "one_liner": "一句话核心事实（15-25字，陈述句）",
  "summary": "新闻梗概（150-300字，不含评论）",
  "key_points": ["关键事实1", "关键事实2", "关键事实3"],
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