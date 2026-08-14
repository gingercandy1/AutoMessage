from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.dialects.postgresql import insert

from app.collectors.base import BaseCollector
from app.database import SessionLocal
from app.database.models import ContentItem
from app.logger import AppLogger

logger = AppLogger.get(__name__)


class LLMLeaderboardCollector(BaseCollector):
    NAME = "llm_leaderboard"

    def __init__(self):
        self.session = None  # injected http session (aiogram/bot/httpx) for tests

    def _normalize_artificial_analysis(self, payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Artificial Analysis leaderboard JSON -> normalized entries."""
        out = []
        for i, row in enumerate(payload or []):
            name = (row.get("modelName") or row.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "model": name,
                "rank": i + 1,
                "score": row.get("arenaScore") or row.get("score"),
                "provider": row.get("organization") or row.get("provider"),
                "provider_url": row.get("modelPage") or row.get("url"),
                "hf_id": row.get("huggingFaceId") or row.get("hf_id"),
                "pricing_usd_per_1m": row.get("pricePrompt") or row.get("pricing"),
            })
        return out

    def _normalize_lmsys(self, payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """LMSYS Chatbot Arena leaderboard rows -> normalized entries."""
        out = []
        for i, row in enumerate(payload or []):
            name = (row.get("model") or row.get("model_name") or "").strip()
            if not name:
                continue
            out.append({
                "model": name,
                "rank": i + 1,
                "score": row.get("score") or row.get("rating"),
                "provider": row.get("organization") or row.get("licese") or None,
                "provider_url": row.get("link") or None,
                "hf_id": row.get("hf_id") or None,
                "pricing_usd_per_1m": None,
            })
        return out

    def normalize(self, source: str, payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if source == "artificial_analysis":
            return self._normalize_artificial_analysis(payload)
        if source == "lmsys":
            return self._normalize_lmsys(payload)
        logger.warning(f"未知 leaderboard 源: {source}")
        return []

    # ---- persistence ----
    def _to_content_items(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        items = []
        for e in entries:
            model = e["model"]
            items.append({
                "source": "llm_leaderboard",
                "external_id": model,
                "category": "llm",
                "title": model,
                "url": e.get("provider_url") or f"https://huggingface.co/{e.get('hf_id')}" if e.get("hf_id") else "",
                "description": f"#{e.get('rank')} · score={e.get('score')}",
                "metrics": {
                    "rank": e.get("rank"),
                    "score": e.get("score"),
                    "pricing_usd_per_1m": e.get("pricing_usd_per_1m"),
                },
                "raw_data": {
                    "provider": e.get("provider"),
                    "provider_url": e.get("provider_url"),
                    "hf_id": e.get("hf_id"),
                    "pricing_usd_per_1m": e.get("pricing_usd_per_1m"),
                },
                "discovered_at": now,
            })
        return items

    def save(self, records: List[Dict[str, Any]]) -> None:
        if not records:
            return
        db = SessionLocal()
        try:
            for rec in records:
                stmt = insert(ContentItem).values(**rec).on_conflict_do_update(
                    index_elements=["source", "external_id"],
                    set_={
                        "title": rec["title"],
                        "url": rec["url"],
                        "description": rec["description"],
                        "metrics": rec["metrics"],
                        "raw_data": rec["raw_data"],
                        "discovered_at": rec["discovered_at"],
                    },
                )
                db.execute(stmt)
            db.commit()
            logger.info(f"✅ 写入 {len(records)} 条 LLM leaderboard")
        except Exception as e:
            db.rollback()
            logger.error(f"❌ 写入 LLM leaderboard 失败: {e}")
            raise
        finally:
            db.close()

    async def fetch(self, config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """采集全部源并落库。config 可覆盖源 payload（测试用）。"""
        all_entries = []
        sources = {
            "artificial_analysis": (config or {}).get("artificial_analysis", []),
            "lmsys": (config or {}).get("lmsys", []),
        }
        for src, payload in sources.items():
            entries = self.normalize(src, payload)
            all_entries.extend(entries)
        items = self._to_content_items(all_entries)
        self.save(items)
        return items

    def configs(self):
        return [{}]
