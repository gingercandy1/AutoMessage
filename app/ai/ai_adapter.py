import time
import httpx
import json
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple, List
from app.config import settings

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """额度耗尽 / 被限流，应该切换到下一个候选"""
    pass


class ProviderUnavailableError(Exception):
    """鉴权失败 / 网络错误 / 返回格式异常等，应该切换到下一个候选"""
    pass


class ProviderCandidate:
    def __init__(self, provider: str, model: str, base_url: str, api_key: Optional[str] = None):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key = api_key

    def __repr__(self):
        return f"{self.provider}:{self.model}"


def raise_for_status(response: httpx.Response) -> None:
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
            raise_for_status(response)

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
    MODELS_URL = f"{BASE_URL}/models"

    FALLBACK_FREE_MODELS = []

    # 类级缓存，避免每次 build_candidates 都打一次远程接口
    _free_models_cache: Optional[List[str]] = None
    _cache_ts: float = 0.0
    _cache_ttl: float = 6 * 3600

    @classmethod
    def _fetch_free_models(cls, api_key: str) -> List[str]:
        now = time.time()
        if cls._free_models_cache is not None and (now - cls._cache_ts) < cls._cache_ttl:
            return cls._free_models_cache

        try:
            resp = httpx.get(
                cls.MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            logger.warning("OpenRouter models fetch failed, fallback to static list: %s", e)
            return cls._free_models_cache or cls.FALLBACK_FREE_MODELS

        free_models = []
        for m in data:
            model_id = m.get("id", "")
            pricing = m.get("pricing", {}) or {}
            is_free_by_price = (
                str(pricing.get("prompt", "-1")) == "0"
                and str(pricing.get("completion", "-1")) == "0"
            )
            is_free_by_suffix = model_id.endswith(":free")
            if model_id and (is_free_by_price or is_free_by_suffix):
                free_models.append(model_id)

        if not free_models:
            logger.warning("OpenRouter free-model filter returned empty, fallback to static list")
            return cls._free_models_cache or cls.FALLBACK_FREE_MODELS

        cls._free_models_cache = free_models
        cls._cache_ts = now
        return free_models

    def build_candidates(self, settings) -> List[ProviderCandidate]:
        api_key = settings.openrouter_api_key
        if not api_key:
            return []

        # 显式配置 openrouter_models 优先，跳过动态拉取
        models = list(getattr(settings, "openrouter_models", None) or [])
        if not models:
            primary = getattr(settings, "openrouter_model", None)
            if primary:
                models.append(primary)
            free_models = self._fetch_free_models(api_key)
            for m in free_models:
                if m not in models:
                    models.append(m)

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


class AgentAdapter(BaseProviderAdapter):
    name = "agent"

    def build_candidates(self, settings) -> List[ProviderCandidate]:
        models = settings.openwebui_model
        token = settings.openwebui_api_key
        base_url = settings.openwebui_api_url
        return [ProviderCandidate("agent", models, base_url, token)]

    async def call(
        self, client: httpx.AsyncClient, candidate: ProviderCandidate, prompt: str
    ) -> Tuple[str, Optional[Dict]]:
        try:
            response = await client.post(
                f"{candidate.base_url}/api/translate",
                json={
                    "model": candidate.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                headers={"Authorization": f"Bearer {candidate.api_key}"},
            )
        except asyncio.TimeoutError:
            raise ProviderUnavailableError(f"Ollama 请求超时 (模型: {candidate.model})")
        except httpx.RequestError as e:
            raise ProviderUnavailableError(f"Ollama 网络错误: {e}")

        if response.status_code != 200:
            raise_for_status(response)

        result = response.json()
        try:
            return result["choices"][0]["message"]["content"], None
        except KeyError:
            raise ProviderUnavailableError(f"Ollama 返回格式异常: {result}")


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
            raise_for_status(response)

        result = response.json()
        try:
            return result["message"]["content"], None
        except KeyError:
            raise ProviderUnavailableError(f"Ollama 返回格式异常: {result}")


PROVIDER_REGISTRY: Dict[str, BaseProviderAdapter] = {
    "agent": AgentAdapter(),
    "openrouter": OpenRouterAdapter(),
    "ollama": OllamaAdapter(),
    "groq": GroqAdapter(),
    "openai": OpenAIAdapter(),
    "opencode": OpenCodeAdapter(),
}