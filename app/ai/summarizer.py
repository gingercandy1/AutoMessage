import json
import logging
import httpx
from typing import Dict, Any, Optional, Tuple, List

from app.ai.ai_adapter import PROVIDER_REGISTRY, ProviderCandidate, ProviderUnavailableError, QuotaExceededError
from app.config import settings

logger = logging.getLogger(__name__)

class ProjectSummarizer:
    def __init__(self, provider: Optional[str] = None):
        self.client: Optional[httpx.AsyncClient] = None
        self.last_candidate: Optional[ProviderCandidate] = None
        self.candidates = self._build_candidates(provider)

        if not self.candidates:
            raise ValueError("没有可用的 provider/model 候选组合，请检查 settings 中的 API Key 配置")

    def _build_candidates(self, provider: Optional[str]) -> List[ProviderCandidate]:
        order = [provider] if provider else (
            getattr(settings, "provider_fallback_order", None) or PROVIDER_REGISTRY.keys()
        )
        candidates: List[ProviderCandidate] = []
        for p in order:
            adapter = PROVIDER_REGISTRY.get(p.lower())
            if adapter is None:
                logger.warning(f"未知 provider，已跳过: {p}")
                continue
            candidates.extend(adapter.build_candidates(settings))
        return candidates

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _ensure_client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def close(self):
        if self.client is not None:
            await self.client.aclose()

    async def _call_candidate(
        self, candidate: ProviderCandidate, prompt: str
    ) -> Tuple[str, Optional[Dict]]:
        adapter = PROVIDER_REGISTRY.get(candidate.provider)
        if adapter is None:
            raise ProviderUnavailableError(f"未知 provider: {candidate.provider}")
        return await adapter.call(self.client, candidate, prompt)

    async def summarize(self, prompt: str) -> Dict[str, Any]:
        logger.info(f"🔢 prompt 长度 {len(prompt)} 字符，候选链: {self.candidates}")
        await self._ensure_client()

        errors_log = []

        for candidate in self.candidates:
            logger.info(f"➡️ 尝试候选: {candidate}")
            data: Optional[Dict[str, Any]] = None
            usage: Optional[Dict] = {}

            try:
                for attempt in range(2):
                    content, usage = await self._call_candidate(candidate, prompt)
                    data = await self._parse_json(content)
                    if "error" in data and data.get("error") == "JSON 解析失败":
                        logger.warning(f"[{candidate}] JSON 解析失败，重试 ({attempt + 1}/2): {data.get('message')}")
                        continue
                    break
            except QuotaExceededError as e:
                logger.warning(f"⚠️ [{candidate}] 额度超限/被限流，切换下一个候选: {e}")
                errors_log.append(f"{candidate}: 额度超限 - {e}")
                continue
            except ProviderUnavailableError as e:
                logger.warning(f"⚠️ [{candidate}] 不可用，切换下一个候选: {e}")
                errors_log.append(f"{candidate}: 不可用 - {e}")
                continue
            except Exception as e:
                logger.exception(f"[{candidate}] 未预期异常，切换下一个候选: {e}")
                errors_log.append(f"{candidate}: 异常 - {e}")
                continue

            if data is None or ("error" in data and data.get("error") == "JSON 解析失败"):
                logger.warning(f"[{candidate}] 连续 2 次 JSON 解析失败，切换下一个候选")
                errors_log.append(f"{candidate}: JSON 连续解析失败")
                continue

            # --- 成功 ---
            self.last_candidate = candidate
            prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
            completion_tokens = usage.get("completion_tokens", 0) if usage else 0
            data["provider"] = candidate.provider
            data["model"] = candidate.model
            score = data.get("score")
            if isinstance(score, (int, float)):
                logger.info(
                    f"📊 [{candidate}] 摘要质量分 score={score} "
                    f"(prompt={prompt_tokens} tok, completion={completion_tokens} tok)"
                )
            return data

        # --- 所有候选都失败 ---
        logger.error(f"❌ 所有候选均失败: {errors_log}")
        return {
            "provider_down": True,
            "error": "所有 provider/model 候选均不可用",
            "details": errors_log,
        }

    async def _parse_json(self, content: str) -> Dict[str, Any]:
        try:
            content = content.strip()
            if content.startswith("```json"):
                content = content.split("```json")[1].split("```")[0]
            elif content.startswith("```"):
                content = content.split("```")[1].split("```")[0]

            content = content.strip()
            return json.loads(content)
        except json.JSONDecodeError as e:
            return {
                "error": "JSON 解析失败",
                "parse_failed": True,
                "raw": content[:500],
                "message": str(e),
            }