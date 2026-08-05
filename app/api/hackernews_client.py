import asyncio
import httpx
from typing import Optional, Dict, Any, List
from app.logger import AppLogger

logger = AppLogger.get(__name__)


class HackerNewsAPIError(Exception):
    """Hacker News API 呼叫發生錯誤時拋出"""


class HackerNewsClient:
    BASE_URL = "https://hacker-news.firebaseio.com/v0"
    ITEM_URL = f"{BASE_URL}/item"
    TOP_STORIES_URL = f"{BASE_URL}/topstories.json"
    NEW_STORIES_URL = f"{BASE_URL}/newstories.json"
    BEST_STORIES_URL = f"{BASE_URL}/beststories.json"
    ASK_STORIES_URL = f"{BASE_URL}/askstories.json"
    SHOW_STORIES_URL = f"{BASE_URL}/showstories.json"
    JOB_STORIES_URL = f"{BASE_URL}/jobstories.json"

    _instance: "HackerNewsClient | None" = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        if getattr(self, "_initialized", False):
            return

        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._initialized = True

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "HackerNewsClient":
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        **kwargs,
    ) -> httpx.Response:
        client = await self._get_client()
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await client.request(method, path, params=params, **kwargs)
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                last_exc = e
                status = e.response.status_code
                if status == 429:
                    wait = 2 ** attempt
                    logger.warning(
                        f"触发速率限制，{wait}s 后重试 ({attempt}/{self.max_retries}): {path}"
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"HTTP 错误 {status}: {path}")
                raise HackerNewsAPIError(f"HTTP {status} 错误: {path}") from e
            except httpx.RequestError as e:
                last_exc = e
                wait = 2 ** attempt
                logger.warning(
                    f"请求失败，{wait}s 后重试 ({attempt}/{self.max_retries}): {e}"
                )
                await asyncio.sleep(wait)

        raise HackerNewsAPIError(f"多次重试后仍失败: {path}") from last_exc

    async def _get_json(self, path: str, params: dict | None = None) -> Any | None:
        resp = await self._request("GET", path, params=params)
        return resp.json()

    # ==================== Stories ====================
    async def get_top_stories(self, limit: int = 500) -> List[int]:
        """获取热门故事 ID 列表"""
        return await self._get_json("/topstories.json") or []

    async def get_new_stories(self, limit: int = 500) -> List[int]:
        """获取最新故事 ID 列表"""
        return await self._get_json("/newstories.json") or []

    async def get_best_stories(self, limit: int = 500) -> List[int]:
        """获取最佳故事 ID 列表"""
        return await self._get_json("/beststories.json") or []

    async def get_ask_stories(self, limit: int = 500) -> List[int]:
        """获取 Ask HN 故事 ID 列表"""
        return await self._get_json("/askstories.json") or []

    async def get_show_stories(self, limit: int = 500) -> List[int]:
        """获取 Show HN 故事 ID 列表"""
        return await self._get_json("/showstories.json") or []

    async def get_job_stories(self, limit: int = 500) -> List[int]:
        """获取 Job HN 故事 ID 列表"""
        return await self._get_json("/jobstories.json") or []

    # ==================== Items ====================
    async def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """获取单个项目详情"""
        return await self._get_json(f"/item/{item_id}.json")

    async def get_items_batch(self, item_ids: List[int], max_concurrent: int = 20) -> List[Optional[Dict[str, Any]]]:
        """批量获取项目详情"""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _fetch(item_id: int):
            async with semaphore:
                try:
                    return await self.get_item(item_id)
                except Exception as e:
                    logger.warning(f"获取项目 {item_id} 失败: {e}")
                    return None

        tasks = [_fetch(item_id) for item_id in item_ids]
        return await asyncio.gather(*tasks)

    # ==================== Users ====================
    async def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """获取用户信息"""
        return await self._get_json(f"/user/{username}.json")


async def main():
    client = HackerNewsClient()
    top_stories = await client.get_top_stories(limit=10)
    print(f"Top 10 story IDs: {top_stories[:10]}")
    
    if top_stories:
        item = await client.get_item(top_stories[0])
        print(f"First item: {item}")


if __name__ == "__main__":
    asyncio.run(main())