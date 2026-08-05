import asyncio
import fnmatch
import os
import httpx
from pathlib import Path
from typing import Any
from app.logger import AppLogger

logger = AppLogger.get(__name__)


class HuggingFaceAPIError(Exception):
    """Hugging Face API 呼叫發生錯誤時拋出"""


class HuggingFaceClient:

    _instance: "HuggingFaceClient | None" = None
    BASE_URL = "https://huggingface.co"
    PIPELINE_MAP = {
        "文字生成": "text-generation",
        "文字转图片": "text-to-image",
        "文字转影片": "text-to-video",
        "语音合成": "text-to-speech",
        "多模态": "image-text-to-text",
    }

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
    ):
        # 單例模式下避免重複初始化覆蓋既有設定
        if getattr(self, "_initialized", False):
            return

        self.token = token or os.getenv("HF_TOKEN")
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._initialized = True

    def _headers(self, extra: dict | None = None) -> dict:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

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

    async def __aenter__(self) -> "HuggingFaceClient":
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def parse_hf_url(self, url):
        url = url.strip()
        url = url.replace("https://", "").replace("http://", "")
        url = url.rstrip("/")
        if "huggingface.co/" in url:
            parts = url.split("huggingface.co/")[1].split("/")
        else:
            parts = url.split("/")
        if len(parts) < 2:
            raise ValueError(f"无效的 GitHub URL: {url}")

        owner = parts[0]
        repo = parts[1]
        return owner, repo

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        **kwargs,
    ) -> httpx.Response:
        client = await self._get_client()
        merged_headers = self._headers(headers)
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await client.request(
                    method, url, params=params, headers=merged_headers, **kwargs
                )
                if resp.status_code == 404:
                    return resp
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                last_exc = e
                status = e.response.status_code
                if status in (401, 403):
                    logger.warning(f"權限不足或 token 無效: {url}")
                    raise HuggingFaceAPIError(
                        f"存取被拒絕 ({status})，請確認 token 是否有效或有權限: {url}"
                    ) from e
                if status == 429:
                    wait = 2 ** attempt
                    logger.warning(
                        f"觸發速率限制，{wait}s 後重試 ({attempt}/{self.max_retries}): {url}"
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"HTTP 錯誤 {status}: {url}")
                raise HuggingFaceAPIError(f"HTTP {status} 錯誤: {url}") from e
            except httpx.RequestError as e:
                last_exc = e
                wait = 2 ** attempt
                logger.warning(
                    f"請求失敗，{wait}s 後重試 ({attempt}/{self.max_retries}): {e}"
                )
                await asyncio.sleep(wait)

        raise HuggingFaceAPIError(f"多次重試後仍失敗: {url}") from last_exc

    async def _get_json(self, url: str, params: dict | None = None) -> Any | None:
        resp = await self._request("GET", url, params=params)
        if resp.status_code == 404:
            return None
        return resp.json()

    def _resolve_url(
        self, repo_id: str, filename: str, revision: str = "main", repo_type: str = "models"
    ) -> str:
        prefix = "" if repo_type == "models" else f"{repo_type}s/"
        return f"{self.BASE_URL}/{prefix}{repo_id}/resolve/{revision}/{filename}"

    # ==================== 模型 (Models) ====================
    async def list_models(
        self,
        *,
        query: str | None = None,
        author: str | None = None,
        pipeline_tag: str | None = None,
        library: str | None = None,
        tags: list[str] | None = None,
        sort: str = "trendingScore",
        direction: int = -1,
        limit: int = 100,
        full: bool = False,
    ) -> list[dict] | None:
        """搜尋 / 列出模型，支援多種篩選條件"""
        params: dict[str, Any] = {"sort": sort, "direction": direction, "limit": limit}
        if query:
            params["search"] = query
        if author:
            params["author"] = author
        if pipeline_tag:
            params["pipeline_tag"] = pipeline_tag
        if library:
            params["library"] = library
        if tags:
            params["tags"] = tags
        if full:
            params["full"] = "true"
        return await self._get_json("/api/models", params=params)

    async def fetch_by_category(self, category_key: str, limit: int = 100, sort: str = "trendingScore") -> list[dict] | None:
        pipeline_tag = self.PIPELINE_MAP.get(category_key)
        if not pipeline_tag:
            raise ValueError(f"不支援的類別: {category_key}")
        return await self.list_models(pipeline_tag=pipeline_tag, sort=sort, limit=limit)

    async def get_model_info(self, model_id: str, revision: str | None = None) -> dict | None:
        path = f"/api/models/{model_id}"
        if revision:
            path += f"/revision/{revision}"
        return await self._get_json(path)

    async def list_model_files(self, model_id: str, revision: str | None = None) -> list[str]:
        """回傳模型 repo 內所有檔案名稱"""
        info = await self.get_model_info(model_id, revision=revision)
        if not info:
            return []
        return [s["rfilename"] for s in info.get("siblings", [])]

    async def get_model_readme(self, model_id: str, revision: str = "main") -> str | None:
        return await self.download_text(model_id, "README.md", revision=revision, repo_type="models")

    # ==================== 資料集 (Datasets) ====================
    async def list_datasets(
        self,
        *,
        query: str | None = None,
        author: str | None = None,
        tags: list[str] | None = None,
        sort: str = "trendingScore",
        direction: int = -1,
        limit: int = 100,
    ) -> list[dict] | None:
        params: dict[str, Any] = {"sort": sort, "direction": direction, "limit": limit}
        if query:
            params["search"] = query
        if author:
            params["author"] = author
        if tags:
            params["tags"] = tags
        return await self._get_json("/api/datasets", params=params)

    async def get_dataset_info(self, dataset_id: str) -> dict | None:
        return await self._get_json(f"/api/datasets/{dataset_id}")

    async def list_dataset_files(self, dataset_id: str) -> list[str]:
        info = await self.get_dataset_info(dataset_id)
        if not info:
            return []
        return [s["rfilename"] for s in info.get("siblings", [])]

    # ==================== Spaces ====================
    async def list_spaces(
        self,
        *,
        query: str | None = None,
        author: str | None = None,
        sort: str = "trendingScore",
        direction: int = -1,
        limit: int = 100,
    ) -> list[dict] | None:
        params: dict[str, Any] = {"sort": sort, "direction": direction, "limit": limit}
        if query:
            params["search"] = query
        if author:
            params["author"] = author
        return await self._get_json("/api/spaces", params=params)

    async def get_space_info(self, space_id: str) -> dict | None:
        return await self._get_json(f"/api/spaces/{space_id}")

    # ==================== Token / 使用者 ====================
    async def whoami(self) -> dict | None:
        """驗證目前 token 是否有效，並回傳所屬使用者/組織資訊"""
        if not self.token:
            raise HuggingFaceAPIError("尚未設定 token，無法呼叫 whoami")
        return await self._get_json("/api/whoami-v2")

    # ==================== 檔案下載 ====================
    async def download_text(
        self, repo_id: str, filename: str, revision: str = "main", repo_type: str = "models"
    ) -> str | None:
        url = self._resolve_url(repo_id, filename, revision, repo_type)
        resp = await self._request("GET", url)
        if resp.status_code == 404:
            return None
        return resp.text

    async def download_bytes(
        self, repo_id: str, filename: str, revision: str = "main", repo_type: str = "models"
    ) -> bytes | None:
        """下載小型檔案並回傳 bytes（大型檔案請使用 download_file 串流下載）"""
        url = self._resolve_url(repo_id, filename, revision, repo_type)
        resp = await self._request("GET", url)
        if resp.status_code == 404:
            return None
        return resp.content

    async def download_file(
        self,
        repo_id: str,
        filename: str,
        local_dir: str | Path,
        revision: str = "main",
        repo_type: str = "models",
        chunk_size: int = 1024 * 1024,
    ) -> Path | None:
        """串流下載檔案至本地目錄，適合大型檔案（如模型權重 .safetensors/.bin）"""
        url = self._resolve_url(repo_id, filename, revision, repo_type)
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        target_path = local_dir / Path(filename).name

        client = await self._get_client()
        headers = self._headers()
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 404:
                    logger.warning(f"檔案不存在: {url}")
                    return None
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(target_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            logger.debug(f"{filename}: {downloaded}/{total} bytes")
            logger.info(f"下載完成: {target_path}")
            return target_path
        except httpx.HTTPStatusError as e:
            logger.error(f"下載失敗 {url}: {e}")
            raise HuggingFaceAPIError(f"下載失敗: {filename}") from e

    async def download_repo(
        self,
        repo_id: str,
        local_dir: str | Path,
        revision: str = "main",
        repo_type: str = "models",
        allow_patterns: list[str] | None = None,
    ) -> list[Path]:
        if repo_type == "models":
            files = await self.list_model_files(repo_id, revision=revision)
        elif repo_type == "dataset":
            files = await self.list_dataset_files(repo_id)
        else:
            raise ValueError(f"不支援的 repo_type: {repo_type}")

        if allow_patterns:
            files = [f for f in files if any(fnmatch.fnmatch(f, p) for p in allow_patterns)]

        downloaded: list[Path] = []
        for filename in files:
            path = await self.download_file(
                repo_id, filename, local_dir, revision=revision, repo_type=repo_type
            )
            if path:
                downloaded.append(path)
        return downloaded

    # ==================== Commits ====================
    async def list_commits(
            self,
            repo_id: str,
            revision: str = "main",
            repo_type: str = "models",
            limit: int = 50,
    ) -> list[dict]:
        prefix = "" if repo_type == "models" else f"{repo_type}s/"
        base_path = f"/api/{prefix}{repo_id}/commits/{revision}"

        commits: list[dict] = []
        params: dict[str, Any] = {}
        url = base_path

        while len(commits) < limit:
            resp = await self._request("GET", url, params=params)
            if resp.status_code == 404:
                logger.warning(f"找不到 commits：{repo_id}")
                break

            data = resp.json()
            page_commits = data if isinstance(data, list) else data.get("commits", [])
            if not page_commits:
                break
            commits.extend(page_commits)
            next_url = self._parse_next_link(resp.headers.get("Link"))
            if not next_url:
                break
            url = next_url
            params = None
        return commits[:limit]

    @staticmethod
    def _parse_next_link(link_header: str | None) -> str | None:
        if not link_header:
            return None
        for part in link_header.split(","):
            segments = part.split(";")
            if len(segments) < 2:
                continue
            url_part = segments[0].strip().strip("<>")
            rel_part = segments[1].strip()
            if rel_part == 'rel="next"':
                return url_part
        return None

    async def list_discussions(
            self,
            repo_id: str,
            repo_type: str = "models",
            limit: int = 10,
    ) -> list[dict]:
        prefix = "" if repo_type == "models" else f"{repo_type}s/"
        path = f"/api/{prefix}{repo_id}/discussions"
        data = await self._get_json(path)
        if not data:
            return []
        discussions = data.get("discussions", [])
        return discussions[:limit]

    async def get_discussion_detail(
            self,
            repo_id: str,
            discussion_num: int,
            repo_type: str = "models",
    ) -> dict | None:
        prefix = "" if repo_type == "models" else f"{repo_type}s/"
        path = f"/api/{prefix}{repo_id}/discussions/{discussion_num}"
        return await self._get_json(path)