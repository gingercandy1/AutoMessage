import httpx
import json
import asyncio
import logging
from typing import Dict, Any, Optional, Tuple
from app.config import settings


logger = logging.getLogger(__name__)


class ProjectSummarizer:

    # 模型定价 (USD per 1M tokens) - 2024 年参考价
    MODEL_PRICING = {
        # OpenRouter models
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
        # OpenAI direct
        "gpt-4o": {"input": 2.5, "output": 10.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.6},
        "gpt-4-turbo": {"input": 10.0, "output": 30.0},
        "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},
    }

    def __init__(self, provider: str = "openrouter"):
        self.provider = provider.lower()
        self.client: Optional[httpx.AsyncClient] = None

        if self.provider == "openrouter":
            self.api_key = settings.openrouter_api_key
            self.model = settings.openrouter_model or "anthropic/claude-3.5-sonnet"
            self.base_url = "https://openrouter.ai/api/v1"
        elif self.provider == "openai":
            self.api_key = settings.openai_api_key
            self.model = settings.openai_model or "gpt-4o-mini"
            self.base_url = "https://api.openai.com/v1"
        elif self.provider == "ollama":
            self.api_key = None
            self.model = settings.ollama_model or "qwen2.5:14b"
            self.base_url = settings.ollama_url or "http://localhost:11434"
        else:
            raise ValueError(f"不支持的 provider: {self.provider}")

    def _calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """根据模型和 token 使用量计算成本 (USD)"""
        pricing = self.MODEL_PRICING.get(self.model)
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

    def _get_headers(self) -> Dict[str, str]:
        if self.provider == "ollama":
            return {"Content-Type": "application/json"}
        else:
            return {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

    async def _ensure_client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=120.0)

    async def summarize(self, prompt) -> Dict[str, Any]:
        await self._ensure_client()
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt.strip()}],
            "temperature": 0.7,
        }

        try:
            if self.provider == "ollama":
                content, usage = await self._summarize_ollama(prompt)
            elif self.provider == "openrouter":
                content, usage = await self._summarize_openrouter(payload)
            elif self.provider == "openai":
                content, usage = await self._summarize_openai(payload)
            else:
                raise ValueError(f"不支持的 provider: {self.provider}")
            
            # First parse attempt
            data = await self._parse_json(content)
            
            # If parse failed, retry once with stricter instruction
            if "error" in data and data.get("error") == "JSON 解析失败":
                logger.warning(f"JSON 解析失败，尝试重试: {data.get('message')}")
                retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation, no code blocks."
                retry_payload = {**payload, "messages": [{"role": "user", "content": retry_prompt.strip()}]}
                
                if self.provider == "ollama":
                    retry_content, retry_usage = await self._summarize_ollama(retry_prompt)
                elif self.provider == "openrouter":
                    retry_content, retry_usage = await self._summarize_openrouter(retry_payload)
                elif self.provider == "openai":
                    retry_content, retry_usage = await self._summarize_openai(retry_payload)
                
                data = await self._parse_json(retry_content)
                if "error" not in data:
                    logger.info("JSON 解析重试成功")
                    usage = retry_usage
            
            if "error" not in data:
                prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
                completion_tokens = usage.get("completion_tokens", 0) if usage else 0
                data["token_cost"] = self._calculate_cost(prompt_tokens, completion_tokens)
                data["provider"] = self.provider
                data["model"] = self.model
            else:
                data["parse_failed"] = True
                logger.error(f"JSON 解析最终失败: {data.get('message')}, raw: {data.get('raw', '')[:200]}")
            return data
        except Exception as e:
            logger.exception(f"Summarize 失败: {e}")
            return {"error": str(e), "provider": self.provider, "token_cost": 0.0}

    async def _summarize_ollama(self, prompt: str) -> Tuple[str, Optional[Dict]]:
        try:
            response = await self.client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False
                },
                headers=self._get_headers()
            )
            response.raise_for_status()
            result = response.json()
            return result["message"]["content"], None
        except asyncio.TimeoutError:
            raise Exception(f"Ollama 请求超时 (模型: {self.model})")
        except Exception as e:
            raise Exception(f"Ollama 请求失败: {str(e)}")

    async def _summarize_openrouter(self, payload: Dict) -> Tuple[str, Dict]:
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._get_headers()
            )
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            usage = result.get("usage", {})
            return content, usage
        except asyncio.TimeoutError:
            raise Exception("OpenRouter 请求超时")
        except Exception as e:
            raise Exception(f"OpenRouter 请求失败: {str(e)}")

    async def _summarize_openai(self, payload: Dict) -> Tuple[str, Dict]:
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._get_headers()
            )
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            usage = result.get("usage", {})
            return content, usage
        except asyncio.TimeoutError:
            raise Exception("OpenAI 请求超时")
        except Exception as e:
            raise Exception(f"OpenAI 请求失败: {str(e)}")

    async def _parse_json(self, content: str) -> Dict[str, Any]:
        try:
            content = content.strip()
            if content.startswith("```json"):
                content = content.split("```json")[1].split("```")[0]
            elif content.startswith("```"):
                content = content.split("```")[1].split("```")[0]

            content = content.strip()
            data = json.loads(content)
            return data

        except json.JSONDecodeError as e:
            return {
                "error": "JSON 解析失败",
                "raw": content[:500],
                "message": str(e)
            }

    async def close(self):
        if self.client is not None:
            await self.client.aclose()

