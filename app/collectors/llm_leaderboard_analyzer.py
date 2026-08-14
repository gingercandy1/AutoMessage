from typing import Any, Dict, Optional

from app.collectors.base import BaseAnalyzer
from app.database import SessionLocal
from app.database.models import ContentItem
from app.logger import AppLogger

logger = AppLogger.get(__name__)


class LLMLeaderboardAnalyzer(BaseAnalyzer):
    NAME = "llm_leaderboard"

    async def _find_item(self, model_id: str) -> Optional[ContentItem]:
        db = SessionLocal()
        try:
            return db.query(ContentItem).filter_by(
                source="llm_leaderboard", external_id=model_id
            ).first()
        finally:
            db.close()

    async def build_context(self, model_id: str) -> str:
        item = await self._find_item(model_id)
        if not item:
            return f"未找到 LLM leaderboard 条目: {model_id}"

        m = item.metrics or {}
        raw = item.raw_data or {}
        hf_id = raw.get("hf_id")
        provider_url = raw.get("provider_url")
        cross_links = []
        if provider_url:
            cross_links.append(f"供应商页: {provider_url}")
        if hf_id:
            cross_links.append(f"HuggingFace: https://huggingface.co/{hf_id}")

        prompt = f"""
你是一位 LLM 评测分析师，请基于下方排行榜数据为该模型生成一段评测摘要（纯 JSON）。

模型: {item.title}
排名: {m.get('rank')}
综合分: {m.get('score')}
供应商: {raw.get('provider')}
定价(每百万 token USD): {m.get('pricing_usd_per_1m')}

交叉链接:
{chr(10).join(cross_links) if cross_links else '无'}

返回 JSON:
{{
  "one_liner": "一句话定位（15-25字）",
  "summary": "评测摘要（80-150字，结合排名/分数/定价判断性价比）",
  "use_cases": "主要适用场景，逗号分隔",
  "pros": ["优点1", "优点2"],
  "cons": ["缺点1"],
  "score": 0,
  "tags": ["tag1", "tag2"],
  "cross_links": {{"huggingface": "{('https://huggingface.co/' + hf_id) if hf_id else ''}", "provider": "{provider_url or ''}"}}
}}
"""
        return prompt
