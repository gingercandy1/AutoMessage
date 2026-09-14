"""LLM 排行榜采集器。
平台（config 可覆盖 payload 便于测试）：
- artificial_analysis: `GET /api/v2/language/models/free`（需 free API key，缺失则跳过）
- lmarena: arena.ai/leaderboard 页面内嵌 JSON 提取（无需 key）
- openrouter: `GET /api/v1/models`（公开 + 现有 key）
- vellum: 页面内嵌 JSON 提取（无需 key）
- seal: Scale AI SEAL 页面内嵌 JSON 提取（无需 key）
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.collectors.base import BaseCollector
from app.config import settings
from app.database import SessionLocal
from app.database.models import ContentItem, MetricSnapshot
from app.logger import AppLogger

logger = AppLogger.get(__name__)

PLATFORM_AA = "artificial_analysis"
PLATFORM_LMARENA = "lmarena"
PLATFORM_OPENROUTER = "openrouter"
PLATFORM_VELLUM = "vellum"
PLATFORM_SEAL = "seal"

ALL_PLATFORMS = [PLATFORM_AA, PLATFORM_LMARENA, PLATFORM_OPENROUTER, PLATFORM_VELLUM, PLATFORM_SEAL]


def platform_external_id(platform: str, model: str) -> str:
    """`平台::模型名`。"""
    return f"{platform}::{model}"


class LLMLeaderboardCollector(BaseCollector):
    NAME = "llm_leaderboard"

    def __init__(self):
        self.session = None  # injected http session (aiogram/bot/httpx) for tests
        self.aa_api_key = settings.artificial_analysis_api_key
        self.aa_base_url = "https://artificialanalysis.ai/api/v2"
        self.lmarena_url = settings.llm_leaderboard_url
        self.vellum_url = settings.vellum_leaderboard_url
        self.seal_url = settings.seal_leaderboard_url
        self.top_n = settings.llm_leaderboard_top_n

    def _normalize_artificial_analysis(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """AA free Data API response -> unified entries.
        payload = {"tier": ..., "pagination": {...}, "data": [...]}
        每行关键字段：id/name/model_creator.name/evaluations.artificial_analysis_intelligence_index/
        pricing.price_1m_input_tokens / price_1m_output_tokens
        """
        rows = payload.get("data", []) if isinstance(payload, dict) else payload or []
        out = []
        ranked = sorted(
            rows,
            key=lambda r: (r.get("evaluations") or {}).get("artificial_analysis_intelligence_index")
            if (r.get("evaluations") or {}).get("artificial_analysis_intelligence_index") is not None
            else -1,
            reverse=True,
        )[: self.top_n]
        for i, row in enumerate(ranked, 1):
            name = (row.get("name") or row.get("id") or "").strip()
            if not name:
                continue
            evals = row.get("evaluations") or {}
            pricing = row.get("pricing") or {}
            creator = row.get("model_creator") or {}
            out.append({
                "model": name,
                "rank": i,
                "score": evals.get("artificial_analysis_intelligence_index"),
                "provider": creator.get("name") or row.get("model_creator") or "unknown",
                "provider_url": f"https://artificialanalysis.ai/models/{row.get('slug', '')}" if row.get("slug") else None,
                "hf_id": None,
                "pricing_usd_per_1m": pricing.get("price_1m_input_tokens"),
                "pricing_output_usd_per_1m": pricing.get("price_1m_output_tokens"),
                "platform": PLATFORM_AA,
            })
        return out

    def _normalize_lmsys(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """LMArena(arena.ai) 页面内嵌 leaderboard entries -> unified entries。
        payload 形如：[{"rank":1,"modelDisplayName":"...","rating":1506.48,
                        "votes":...,"modelOrganization":"...","modelUrl":"...",
                        "inputPricePerMillion":...,"outputPricePerMillion":...}]
        """
        entries = payload if isinstance(payload, list) else (payload.get("entries", []) if isinstance(payload, dict) else [])
        out = []
        for e in (entries or [])[: self.top_n]:
            name = (e.get("modelDisplayName") or e.get("model") or "").strip()
            if not name:
                continue
            out.append({
                "model": name,
                "rank": e.get("rank"),
                "score": e.get("rating"),
                "provider": e.get("modelOrganization") or e.get("organization"),
                "provider_url": e.get("modelUrl"),
                "hf_id": None,
                "pricing_usd_per_1m": e.get("inputPricePerMillion"),
                "pricing_output_usd_per_1m": e.get("outputPricePerMillion"),
                "votes": e.get("votes"),
                "platform": PLATFORM_LMARENA,
            })
        return out

    def _normalize_openrouter(self, payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """OpenRouter /api/v1/models response ["data"] -> unified entries。
        """
        rows = payload if isinstance(payload, list) else (payload.get("data", []) if isinstance(payload, dict) else [])
        ranked = sorted(
            rows,
            key=lambda r: r.get("top_choice") if r.get("top_choice") is not None else (r.get("created") or 0),
            reverse=True,
        )[: self.top_n]
        out = []
        for i, row in enumerate(ranked, 1):
            model_id = (row.get("id") or row.get("name") or "").strip()
            if not model_id:
                continue
            pricing = row.get("pricing") or {}
            out.append({
                "model": model_id,
                "rank": i,
                "score": row.get("top_choice") or row.get("context_length"),
                "provider": row.get("vendor") if isinstance(row.get("vendor"), str) else (row.get("vendor") or {}).get("name"),
                "provider_url": None,
                "hf_id": None,
                "pricing_usd_per_1m": pricing.get("prompt"),
                "pricing_output_usd_per_1m": pricing.get("completion"),
                "platform": PLATFORM_OPENROUTER,
            })
        return out

    def _normalize_vellum(self, payload: Any) -> List[Dict[str, Any]]:
        """Vellum 页面内嵌 JSON -> unified entries。
        结构不固定，兼容多种字段名（model/name/modelDisplayName/rank/score/rating/llm 等）。
        """
        entries = self._coerce_entries(payload)
        out = []
        for i, e in enumerate(entries[: self.top_n], 1):
            name = (e.get("model") or e.get("modelDisplayName") or e.get("name") or e.get("llm") or "").strip()
            if not name:
                continue
            out.append({
                "model": name,
                "rank": e.get("rank") or i,
                "score": e.get("score") or e.get("rating") or e.get("ai_quality"),
                "provider": e.get("provider") or e.get("organization"),
                "provider_url": e.get("url") or e.get("link"),
                "hf_id": None,
                "pricing_usd_per_1m": e.get("pricing") or e.get("input_price"),
                "pricing_output_usd_per_1m": e.get("output_price"),
                "platform": PLATFORM_VELLUM,
            })
        return out

    def _normalize_seal(self, payload: Any) -> List[Dict[str, Any]]:
        """Scale AI SEAL 页面内嵌 JSON -> unified entries。结构兼容策略同 vellum。"""
        entries = self._coerce_entries(payload)
        out = []
        for i, e in enumerate(entries[: self.top_n], 1):
            name = (e.get("model") or e.get("modelDisplayName") or e.get("name") or e.get("llm") or "").strip()
            if not name:
                continue
            out.append({
                "model": name,
                "rank": e.get("rank") or i,
                "score": e.get("score") or e.get("rating"),
                "provider": e.get("provider") or e.get("organization"),
                "provider_url": e.get("url") or e.get("link"),
                "hf_id": None,
                "pricing_usd_per_1m": e.get("pricing") or e.get("input_price"),
                "pricing_output_usd_per_1m": e.get("output_price"),
                "platform": PLATFORM_SEAL,
            })
        return out

    @staticmethod
    def _coerce_entries(payload: Any) -> List[Dict[str, Any]]:
        """把各种可能的内嵌结构规整为 entries 列表。"""
        if isinstance(payload, list):
            return [e for e in payload if isinstance(e, dict)]
        if isinstance(payload, dict):
            for key in ("entries", "models", "leaderboard", "data", "rows", "results"):
                v = payload.get(key)
                if isinstance(v, list):
                    return [e for e in v if isinstance(e, dict)]
                if isinstance(v, dict):
                    return [e for e in v.get("entries", []) if isinstance(e, dict)]
            # 直接是模型字典（如 {"gpt-4o": {...}}）
            out = []
            for name, v in payload.items():
                if isinstance(v, dict):
                    merged = dict(v)
                    merged.setdefault("model", name)
                    out.append(merged)
            return out
        return []

    async def _fetch_artificial_analysis(self) -> List[Dict[str, Any]]:
        if not (self.aa_api_key and self.aa_api_key.strip()):
            logger.warning("⚠️ Artificial Analysis API key 未配置，跳过该平台（T07）。")
            return []
        url = f"{self.aa_base_url}/language/models/free"
        headers = {"x-api-key": self.aa_api_key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, params={"page": 1}, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        return self._normalize_artificial_analysis(payload)

    async def _fetch_lmarena(self) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True,
                                     headers={"User-Agent": "AIProjectBot/1.0 (leaderboard collector)"}) as client:
            resp = await client.get(self.lmarena_url)
            resp.raise_for_status()
            html = resp.text
        entries = self._extract_next_f_entries(html, arena_slug="text")
        if not entries:
            # 兜底：直接找 entries 数组
            entries = self._extract_any_entries(html)
        return self._normalize_lmsys({"entries": entries})

    @staticmethod
    def _extract_next_f_entries(html: str, arena_slug: str = "text") -> List[Dict[str, Any]]:
        """从 Next.js RSC push 块中提取指定 arena 的 leaderboard entries。"""
        chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html)
        for chunk in chunks:
            try:
                text = json.loads(chunk)
            except (json.JSONDecodeError, TypeError):
                continue
            if f'"arenaSlug":"{arena_slug}"' not in text:
                continue
            # 找 "entries":[...] 平衡括号提取
            start = text.find('"entries":[')
            if start < 0:
                continue
            arr_start = text.find("[", start)
            depth = 0
            in_str = False
            esc = False
            end = -1
            for i in range(arr_start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end < 0:
                continue
            try:
                return json.loads(text[arr_start:end])
            except (json.JSONDecodeError, TypeError):
                continue
        return []

    @staticmethod
    @staticmethod
    def _match_bracketed(text: str, start: int) -> str:
        """从 text[start]（必须是 '[' 或 '{'）开始，做括号匹配，返回完整片段。失败返回 ''。"""
        if start < 0 or start >= len(text) or text[start] not in ("[", "{"):
            return ""
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return ""

    @staticmethod
    def _extract_any_entries(html: str) -> List[Dict[str, Any]]:
        """兜底：从 HTML 中任意位置提取第一个 list-of-dicts（entries/models/leaderboard/data/rows）。

        修复：① 先 html.unescape（真实页面 JSON 在 HTML 里常被转义）；
        ② 候选键覆盖 _find_list 的同款键名，而非只认 "entries":[；
        ③ 找到数组后 json.loads 失败不早退，继续试下一个候选。
        """
        import html as _html
        decoded = _html.unescape(html)
        for key in ('"entries":[', '"models":[', '"leaderboard":[', '"data":[', '"rows":['):
            idx = decoded.find(key)
            if idx < 0:
                continue
            arr_start = decoded.find("[", idx)
            frag = LLMLeaderboardCollector._match_bracketed(decoded, arr_start)
            if not frag:
                continue
            try:
                parsed = json.loads(frag)
            except (json.JSONDecodeError, TypeError):
                continue
            found = LLMLeaderboardCollector._find_list(parsed)
            if found:
                return found
        return []

    async def _fetch_openrouter(self) -> List[Dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if settings.openrouter_api_key:
            headers["Authorization"] = f"Bearer {settings.openrouter_api_key}"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        return self._normalize_openrouter(payload)

    async def _fetch_vellum(self) -> List[Dict[str, Any]]:
        payload = await self._fetch_embedded_json(self.vellum_url)
        return self._normalize_vellum(payload)

    async def _fetch_seal(self) -> List[Dict[str, Any]]:
        payload = await self._fetch_embedded_json(self.seal_url)
        return self._normalize_seal(payload)

    async def _fetch_embedded_json(self, url: str) -> Any:
        """从页面里提取内嵌 JSON（__NEXT_DATA__ 或 __next_f 或 entries）。"""
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True,
                                     headers={"User-Agent": "AIProjectBot/1.0 (leaderboard collector)"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        # 1) __NEXT_DATA__
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return self._find_list(data)
            except (json.JSONDecodeError, TypeError):
                pass
        # 2) __next_f push chunks
        chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html)
        for chunk in chunks:
            try:
                text = json.loads(chunk)
            except (json.JSONDecodeError, TypeError):
                continue
            entries = self._extract_entries_from_text(text)
            if entries:
                return {"entries": entries}
        # 3) 兜底任何 entries
        return {"entries": self._extract_any_entries(html)}

    @staticmethod
    def _extract_entries_from_text(text: str) -> List[Dict[str, Any]]:
        start = text.find('"entries":[')
        if start < 0:
            return []
        arr_start = text.find("[", start)
        depth = 0
        in_str = False
        esc = False
        for i in range(arr_start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[arr_start : i + 1])
                    except (json.JSONDecodeError, TypeError):
                        return []
        return []

    @staticmethod
    def _find_list(obj: Any) -> List[Dict[str, Any]]:
        """深度优先找第一个 list-of-dicts，作为候选 entries。"""
        if isinstance(obj, list):
            if obj and all(isinstance(x, dict) for x in obj):
                return obj
            for x in obj:
                found = LLMLeaderboardCollector._find_list(x)
                if found:
                    return found
        elif isinstance(obj, dict):
            for k in ("entries", "models", "leaderboard", "data", "rows"):
                if k in obj:
                    found = LLMLeaderboardCollector._find_list(obj[k])
                    if found:
                        return found
            for v in obj.values():
                found = LLMLeaderboardCollector._find_list(v)
                if found:
                    return found
        return []

    def _to_content_items(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        items = []
        for e in entries:
            platform = e.get("platform", "unknown")
            model = e["model"]
            external_id = platform_external_id(platform, model)
            hf_id = e.get("hf_id")
            url = e.get("provider_url") or (f"https://huggingface.co/{hf_id}" if hf_id else "")
            items.append({
                "source": "llm_leaderboard",
                "external_id": external_id,
                "category": "llm",
                "title": model,
                "url": url,
                "description": f"#{e.get('rank')} · {platform} · score={e.get('score')}",
                # 榜单数据本身就是完整信息，不进 LLM 摘要批处理，采集即完成。
                "processed": 1,
                "metrics": {
                    "platform": platform,
                    "rank": e.get("rank"),
                    "score": e.get("score"),
                    "pricing_usd_per_1m": e.get("pricing_usd_per_1m"),
                    "pricing_output_usd_per_1m": e.get("pricing_output_usd_per_1m"),
                },
                "raw_data": {
                    "platform": platform,
                    "provider": e.get("provider"),
                    "provider_url": e.get("provider_url"),
                    "hf_id": hf_id,
                    "votes": e.get("votes"),
                },
                "discovered_at": now,
            })
        return items

    def save(self, records: List[Dict[str, Any]]) -> None:
        if not records:
            return
        db = SessionLocal()
        now = datetime.now(timezone.utc)
        today = now.date()
        try:
            for rec in records:
                content_item_id = None
                try:
                    # SAVEPOINT：url 唯一约束冲突时只回滚本条语句，事务不进入
                    # aborted 状态，恢复查询与后续记录可继续（否则 psycopg2 报
                    # InFailedSqlTransaction 掩盖真实冲突）。
                    with db.begin_nested():
                        stmt = insert(ContentItem).values(**rec).on_conflict_do_update(
                            index_elements=["source", "external_id"],
                            set_={
                                "title": rec["title"],
                                "url": rec["url"],
                                "description": rec["description"],
                                "processed": rec["processed"],
                                "metrics": rec["metrics"],
                                "raw_data": rec["raw_data"],
                                "discovered_at": rec["discovered_at"],
                            },
                        )
                        db.execute(stmt)
                        ci = db.query(ContentItem.id).filter_by(
                            source=rec["source"], external_id=rec["external_id"]
                        ).first()
                        content_item_id = ci.id if ci else None
                except IntegrityError:
                    # 同一官网 URL 被另一平台行占用（leaderboard 多平台常指向同一模型官网）。
                    # 复用已有行的 id，跳过 ContentItem 新建，仍写入本平台的 MetricSnapshot。
                    logger.warning(f"⚠️ ContentItem URL 冲突，复用已有行: {rec['url']}")
                    existing = db.query(ContentItem.id).filter_by(url=rec["url"]).first()
                    content_item_id = existing.id if existing else None
                    if content_item_id is None:
                        continue

                # MetricSnapshot（ADR-0002 键：(source, external_id, captured_date)）
                snap = insert(MetricSnapshot).values(
                    content_item_id=content_item_id,
                    source=rec["source"],
                    external_id=rec["external_id"],
                    category="llm",
                    rank=rec["metrics"].get("rank"),
                    metrics=rec["metrics"],
                    captured_date=today,
                    captured_at=now,
                ).on_conflict_do_update(
                    index_elements=["source", "external_id", "captured_date"],
                    set_={
                        "content_item_id": content_item_id,
                        "rank": rec["metrics"].get("rank"),
                        "metrics": rec["metrics"],
                        "captured_at": now,
                    },
                )
                db.execute(snap)
            db.commit()
            logger.info(f"✅ 写入 {len(records)} 条 LLM leaderboard")
        except Exception as e:
            db.rollback()
            logger.error(f"❌ 写入 LLM leaderboard 失败: {e}")
            raise
        finally:
            db.close()

    async def fetch(self, config: Optional[Dict[str, Any]] = None) -> int:
        """采集全部平台并落库。
        config 可选覆盖各平台 payload（测试用）：{"artificial_analysis": {...}, "lmarena": [...]}
        或 {"platforms": ["lmarena", "openrouter"]} 限制平台。
        """
        config = config or {}
        # 若 config 里有 payload 键（测试注入），只跑这些平台；否则跑全部
        injected = [k for k in ALL_PLATFORMS if k in config and isinstance(config[k], (list, dict))]
        requested = config.get("platforms") or (injected if injected else ALL_PLATFORMS)
        fetchers = {
            PLATFORM_AA: self._fetch_artificial_analysis,
            PLATFORM_LMARENA: self._fetch_lmarena,
            PLATFORM_OPENROUTER: self._fetch_openrouter,
        }

        all_entries = []
        for platform in requested:
            if platform in config and isinstance(config[platform], (list, dict)):
                # 测试注入的 payload 直接走 normalize
                payload = config[platform]
                if platform == PLATFORM_AA:
                    entries = self._normalize_artificial_analysis(payload)
                elif platform == PLATFORM_LMARENA:
                    entries = self._normalize_lmsys(payload)
                elif platform == PLATFORM_OPENROUTER:
                    entries = self._normalize_openrouter(payload)
                elif platform == PLATFORM_VELLUM:
                    entries = self._normalize_vellum(payload)
                else:
                    entries = self._normalize_seal(payload)
            else:
                try:
                    entries = await fetchers[platform]()
                except Exception as e:
                    logger.warning(f"⚠️ 排行榜平台 {platform} 采集失败: {e}")
                    continue
            all_entries.extend(entries)

        items = self._to_content_items(all_entries)
        self.save(items)
        return items

    def configs(self):
        return [{"platforms": ALL_PLATFORMS}]
