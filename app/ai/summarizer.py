import httpx
import json
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple, List
from app.config import settings

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """额度耗尽 / 被限流，应该切换到下一个候选"""
    pass


class ProviderUnavailableError(Exception):
    """鉴权失败 / 网络错误 / 返回格式异常等，应该切换到下一个候选"""
    pass


# ---------------------------------------------------------------------------
# 候选数据结构
# ---------------------------------------------------------------------------
class ProviderCandidate:
    def __init__(self, provider: str, model: str, base_url: str, api_key: Optional[str] = None):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key = api_key

    def __repr__(self):
        return f"{self.provider}:{self.model}"


# ---------------------------------------------------------------------------
# 公共工具函数
# ---------------------------------------------------------------------------
def raise_for_status(candidate: ProviderCandidate, response: httpx.Response) -> None:
    """统一把 HTTP 错误分类成 QuotaExceededError / ProviderUnavailableError"""
    status = response.status_code
    try:
        body = response.json()
        err_msg = json.dumps(body.get("error", body), ensure_ascii=False)
    except Exception:
        err_msg = response.text[:300]

    text_lower = err_msg.lower()
    quota_keywords = ["insufficient_quota", "quota", "rate_limit", "rate limit", "credits", "too many requests"]

    if status in (429, 402) or any(k in text_lower for k in quota_keywords):
        raise QuotaExceededError(f"HTTP {status}: {err_msg}")
    if status in (401, 403):
        raise ProviderUnavailableError(f"HTTP {status} 鉴权失败/项目被拒: {err_msg}")
    raise ProviderUnavailableError(f"HTTP {status}: {err_msg}")


# ---------------------------------------------------------------------------
# Adapter 基类
# ---------------------------------------------------------------------------
class BaseProviderAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def build_candidates(self, settings) -> List[ProviderCandidate]:
        """根据 settings 生成这个平台的候选列表；没配 key 就返回 []"""
        raise NotImplementedError

    @abstractmethod
    async def call(
        self, client: httpx.AsyncClient, candidate: ProviderCandidate, prompt: str
    ) -> Tuple[str, Optional[Dict]]:
        """真正发起请求，返回 (content文本, usage字典或None)"""
        raise NotImplementedError


class OpenAICompatibleAdapter(BaseProviderAdapter):
    """
    openrouter / openai / groq / opencode 等所有兼容 OpenAI /chat/completions
    协议的平台的公共实现。子类只需要实现 build_candidates。
    """

    async def call(
        self, client: httpx.AsyncClient, candidate: ProviderCandidate, prompt: str
    ) -> Tuple[str, Dict]:
        payload = {
            "model": candidate.model,
            "messages": [{"role": "user", "content": prompt.strip()}],
            "temperature": 0.7,
        }
        headers = {"Content-Type": "application/json"}
        if candidate.api_key:
            headers["Authorization"] = f"Bearer {candidate.api_key}"
        if candidate.provider == "openrouter":
            # OpenRouter 建议附带这两个头，有助于免费模型的额度统计与限流识别
            headers["HTTP-Referer"] = getattr(settings, "app_url", "http://localhost")
            headers["X-Title"] = getattr(settings, "app_name", "ProjectSummarizer")

        try:
            response = await client.post(
                f"{candidate.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
        except asyncio.TimeoutError:
            raise ProviderUnavailableError(f"{candidate.provider} 请求超时")
        except httpx.RequestError as e:
            raise ProviderUnavailableError(f"{candidate.provider} 网络错误: {e}")

        if response.status_code != 200:
            raise_for_status(candidate, response)

        result = response.json()
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise ProviderUnavailableError(f"{candidate.provider} 返回格式异常: {result}")
        usage = result.get("usage", {})
        return content, usage


class OpenRouterAdapter(OpenAICompatibleAdapter):
    name = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1"

    # 免费模型池：OpenRouter 的 :free 名单会经常变动（有的模型会被下线/改名），
    # 建议定期去 https://openrouter.ai/models?max_price=0 核实一下还在不在。
    # 注意：deepseek / gemini / mistral 的免费版目前（2026-08）在 OpenRouter 上已不可用，
    # 不要再往这个列表里加这几家的 :free 型号，加了只会白白多一次 404 重试。
    DEFAULT_FREE_MODELS = [
        "qwen/qwen3-coder:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-oss-120b:free",
    ]

    def build_candidates(self, settings) -> List[ProviderCandidate]:
        api_key = settings.openrouter_api_key
        if not api_key:
            return []
        # 如果 settings 里显式配置了 openrouter_models，用配置的；
        # 否则第一顺位放用户指定的付费/主力模型（如果配了 openrouter_model），
        # 再依次追加免费模型池兜底。
        models = list(getattr(settings, "openrouter_models", None) or [])
        if not models:
            primary = getattr(settings, "openrouter_model", None)
            if primary:
                models.append(primary)
            models.extend(self.DEFAULT_FREE_MODELS)
        return [ProviderCandidate("openrouter", m, self.BASE_URL, api_key) for m in models]


class GroqAdapter(OpenAICompatibleAdapter):
    name = "groq"
    BASE_URL = "https://api.groq.com/openai/v1"

    def build_candidates(self, settings) -> List[ProviderCandidate]:
        api_key = getattr(settings, "groq_api_key", None)
        if not api_key:
            return []
        models = getattr(settings, "groq_models", None) or [
            getattr(settings, "groq_model", None) or "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "gemma2-9b-it",
        ]
        base_url = getattr(settings, "groq_base_url", None) or self.BASE_URL
        return [ProviderCandidate("groq", m, base_url, api_key) for m in models]


class OpenAIAdapter(OpenAICompatibleAdapter):
    name = "openai"

    def build_candidates(self, settings) -> List[ProviderCandidate]:
        api_key = settings.openai_api_key
        if not api_key:
            return []
        models = getattr(settings, "openai_models", None) or [
            settings.openai_model or "gpt-4o-mini",
            "gpt-4o",
            "gpt-3.5-turbo",
        ]
        base_url = settings.openai_base_url or "https://api.openai.com/v1"
        return [ProviderCandidate("openai", m, base_url, api_key) for m in models]


class OpenCodeAdapter(OpenAICompatibleAdapter):
    name = "opencode"

    def build_candidates(self, settings) -> List[ProviderCandidate]:
        api_key = settings.openai_api_key
        models = getattr(settings, "opencode_models", None) or [
            settings.opencode_model or settings.openai_model or "gpt-4o-mini"
        ]
        base_url = settings.opencode_base_url or "http://localhost:2424/v1"
        return [ProviderCandidate("opencode", m, base_url, api_key) for m in models]


class OllamaAdapter(BaseProviderAdapter):
    name = "ollama"

    def build_candidates(self, settings) -> List[ProviderCandidate]:
        models = getattr(settings, "ollama_models", None) or [
            settings.ollama_model or "qwen2.5:14b"
        ]
        base_url = settings.ollama_url or "http://localhost:11434"
        # 本地模型不需要 api_key
        return [ProviderCandidate("ollama", m, base_url, None) for m in models]

    async def call(
        self, client: httpx.AsyncClient, candidate: ProviderCandidate, prompt: str
    ) -> Tuple[str, Optional[Dict]]:
        try:
            response = await client.post(
                f"{candidate.base_url}/api/chat",
                json={
                    "model": candidate.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                headers={"Content-Type": "application/json"},
            )
        except asyncio.TimeoutError:
            raise ProviderUnavailableError(f"Ollama 请求超时 (模型: {candidate.model})")
        except httpx.RequestError as e:
            raise ProviderUnavailableError(f"Ollama 网络错误: {e}")

        if response.status_code != 200:
            raise_for_status(candidate, response)

        result = response.json()
        try:
            return result["message"]["content"], None
        except KeyError:
            raise ProviderUnavailableError(f"Ollama 返回格式异常: {result}")


# ---------------------------------------------------------------------------
# 注册表：新增平台只需要在这里加一行
# ---------------------------------------------------------------------------

PROVIDER_REGISTRY: Dict[str, BaseProviderAdapter] = {
    "openrouter": OpenRouterAdapter(),
    "ollama": OllamaAdapter(),
    "groq": GroqAdapter(),
    "openai": OpenAIAdapter(),
    "opencode": OpenCodeAdapter(),
}

# 默认降级顺序：优先免费额度大的 OpenRouter，再本地 Ollama（零成本兜底），
# 最后才是 Groq / OpenAI / OpenCode 这些国内直连可能不稳/有成本的平台。
# 可以在 settings.provider_fallback_order 里覆盖这个默认顺序。
DEFAULT_PROVIDER_ORDER = ["openrouter", "ollama", "groq", "openai", "opencode"]


# ---------------------------------------------------------------------------
# 编排层
# ---------------------------------------------------------------------------

class ProjectSummarizer:
    # 仅用于成本估算展示，不影响调用逻辑；免费模型统一是 0
    MODEL_PRICING = {
        # --- OpenRouter 付费 ---
        "anthropic/claude-3.5-sonnet": {"input": 3.0, "output": 15.0},
        "anthropic/claude-3-opus": {"input": 15.0, "output": 75.0},
        "anthropic/claude-3-haiku": {"input": 0.25, "output": 1.25},
        "openai/gpt-4o": {"input": 2.5, "output": 10.0},
        "openai/gpt-4o-mini": {"input": 0.15, "output": 0.6},
        "openai/gpt-4-turbo": {"input": 10.0, "output": 30.0},
        "meta-llama/llama-3.1-405b-instruct": {"input": 2.7, "output": 2.7},
        "meta-llama/llama-3.1-70b-instruct": {"input": 0.8, "output": 0.8},
        "meta-llama/llama-3.1-8b-instruct": {"input": 0.1, "output": 0.1},
        "google/gemini-pro-1.5": {"input": 1.25, "output": 5.0},

        # --- OpenRouter 免费（当前仍在线的，见 OpenRouterAdapter.DEFAULT_FREE_MODELS）---
        "qwen/qwen3-coder:free": {"input": 0.0, "output": 0.0},
        "meta-llama/llama-3.3-70b-instruct:free": {"input": 0.0, "output": 0.0},
        "openai/gpt-oss-120b:free": {"input": 0.0, "output": 0.0},

        # --- OpenAI 直连 ---
        "gpt-4o": {"input": 2.5, "output": 10.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.6},
        "gpt-4-turbo": {"input": 10.0, "output": 30.0},
        "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},

        # --- Groq（价格仅供参考，以 Groq 官网 pricing 页为准）---
        "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
        "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
        "gemma2-9b-it": {"input": 0.20, "output": 0.20},
        "gpt-oss-20b": {"input": 0.075, "output": 0.30},
        "gpt-oss-120b": {"input": 0.15, "output": 0.60},

        # --- Ollama 本地，永远免费 ---
        "qwen2.5:14b": {"input": 0.0, "output": 0.0},
    }

    def __init__(
        self,
        provider: Optional[str] = None,
        candidates: Optional[List[ProviderCandidate]] = None,
    ):
        self.client: Optional[httpx.AsyncClient] = None
        self.last_candidate: Optional[ProviderCandidate] = None
        self.candidates = candidates if candidates else self._build_candidates(provider)

        if not self.candidates:
            raise ValueError("没有可用的 provider/model 候选组合，请检查 settings 中的 API Key 配置")

    def _build_candidates(self, provider: Optional[str]) -> List[ProviderCandidate]:
        order = [provider] if provider else (
            getattr(settings, "provider_fallback_order", None) or DEFAULT_PROVIDER_ORDER
        )
        candidates: List[ProviderCandidate] = []
        for p in order:
            adapter = PROVIDER_REGISTRY.get(p.lower())
            if adapter is None:
                logger.warning(f"未知 provider，已跳过: {p}")
                continue
            candidates.extend(adapter.build_candidates(settings))
        return candidates

    def _calculate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pricing = self.MODEL_PRICING.get(model)
        if not pricing:
            return 0.0
        input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
        output_cost = (completion_tokens / 1_000_000) * pricing["output"]
        return round(input_cost + output_cost, 6)

    async def __aenter__(self):
        self.client = httpx.AsyncClient(timeout=120.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _ensure_client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=120.0)

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
            data["token_cost"] = self._calculate_cost(candidate.model, prompt_tokens, completion_tokens)
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
            "error": "所有 provider/model 候选均不可用",
            "details": errors_log,
            "token_cost": 0.0,
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