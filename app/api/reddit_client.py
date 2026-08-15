"""Reddit 公开 .json 接口客户端（T02）。

无需 key：https://www.reddit.com/r/{sub}/.json
规范 User-Agent + 限流可控；单子版块失败返回空列表，不抛出中断采集。
"""
import asyncio
import httpx
from typing import Any, Dict, List
from app.logger import AppLogger

logger = AppLogger.get(__name__)


class RedditAPIError(Exception):
    """Reddit API 呼叫發生錯誤時拋出"""


class RedditClient:
    BASE_URL = "https://www.reddit.com"

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str = "AIProjectBot/1.0 (news collector; contact: github.com/ai-project-bot)",
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "RedditClient":
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _get_json(self, path: str, params: dict | None = None) -> Any | None:
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await client.get(path, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                last_exc = e
                status = e.response.status_code
                if status == 429:
                    wait = 2 ** attempt
                    logger.warning(
                        f"Reddit 速率限制，{wait}s 后重试 ({attempt}/{self.max_retries}): {path}"
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(f"Reddit HTTP 错误 {status}: {path}")
                return None
            except httpx.RequestError as e:
                last_exc = e
                wait = 2 ** attempt
                logger.warning(
                    f"Reddit 请求失败，{wait}s 后重试 ({attempt}/{self.max_retries}): {e}"
                )
                await asyncio.sleep(wait)
        logger.warning(f"Reddit 多次重试后仍失败: {path}")
        return None

    async def get_subreddit_posts(self, subreddit: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取子版块 hot 帖子（T02）。失败返回 []，不中断。"""
        path = f"/r/{subreddit}/.json"
        data = await self._get_json(path, params={"limit": limit, "raw_json": 1})
        if not data:
            return []
        children = data.get("data", {}).get("children", [])
        posts = [c["data"] for c in children if c.get("kind") == "t3" and isinstance(c.get("data"), dict)]
        logger.info(f"✅ Reddit r/{subreddit}: {len(posts)} 条")
        return posts
