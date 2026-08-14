import asyncio
import re
import httpx
from typing import Optional, Dict, Any, List, Tuple
from app.logger import AppLogger

logger = AppLogger.get(__name__)

class GitHubRateLimitError(Exception):
    """限流触发时抛出，携带 reset 时间供上层决定怎么处理（等待/降级/报警）"""
    def __init__(self, message: str, reset_at: Optional[float] = None):
        super().__init__(message)
        self.reset_at = reset_at


class GitHubClient:
    _instance = None
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self,
                 token: Optional[str] = None,
                 base_url: str = "https://api.github.com",
                 timeout: float = 30.0):
        if hasattr(self, "_initialized"):
            return

        self._initialized = True
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}" if token else "",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _request(
            self,
            method: str,
            path: str,
            *,
            params: Optional[dict] = None,
            headers: Optional[dict] = None,
            allow_404: bool = False,
    ) -> Optional[httpx.Response]:
        resp = await self._client.request(method, path, params=params, headers=headers)

        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < 50:
            logger.warning(f"⚠️ GitHub API 配额剩余 {remaining}，接近上限")

        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset_at = resp.headers.get("X-RateLimit-Reset")
            raise GitHubRateLimitError(
                "触发限流", reset_at=float(reset_at) if reset_at else None
            )

        if resp.status_code == 404 and allow_404:
            return None

        resp.raise_for_status()
        return resp

    @staticmethod
    def _parse_link_header(link_header: Optional[str]) -> Dict[str, int]:
        pages = {}
        if not link_header:
            return pages
        for part in link_header.split(","):
            match = re.search(r'page=(\d+)>; rel="(\w+)"', part)
            if match:
                pages[match.group(2)] = int(match.group(1))
        return pages

    async def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        resp = await self._request("GET", f"/repos/{owner}/{repo}")
        return resp.json()

    async def get_readme(self, owner: str, repo: str) -> str:
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/readme",
            headers={"Accept": "application/vnd.github.v3.raw"},
            allow_404=True,
        )
        return resp.text if resp else ""

    async def get_rate_limit(self) -> Dict[str, Any]:
        resp = await self._request("GET", "/rate_limit")
        return resp.json()["resources"]["core"]

    async def get_topics(self, owner: str, repo: str) -> List[str]:
        """仓库主题标签（Topics）—— 需在请求头声明应用/vnd.github.mercy-preview+json。"""
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/topics",
            headers={"Accept": "application/vnd.github.mercy-preview+json"},
            allow_404=True,
        )
        if not resp:
            return []
        data = resp.json()
        return data.get("names", []) if isinstance(data, dict) else []

    async def get_contributors(self, owner: str, repo: str, per_page: int = 10) -> List[Dict[str, Any]]:
        """贡献者列表（含贡献数），用于社区健康度评估。"""
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/contributors",
            params={"per_page": per_page}, allow_404=True,
        )
        if not resp:
            return []
        return resp.json() if isinstance(resp.json(), list) else []

    async def get_languages(self, owner: str, repo: str) -> Dict[str, int]:
        """语言占比（字节数），用于判断技术栈构成。"""
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/languages", allow_404=True
        )
        if not resp:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def list_commits(
            self,
            owner: str,
            repo: str,
            since: Optional[str] = None,
            until: Optional[str] = None,
            per_page: int = 100,
            page: int = 1,
    ) -> List[Dict[str, Any]]:
        params = {"per_page": per_page, "page": page}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        resp = await self._request("GET", f"/repos/{owner}/{repo}/commits", params=params, allow_404=True)
        return resp.json() if resp else []

    async def get_commit_activity_stats(self, owner: str, repo: str, max_retries: int = 3) -> List[Dict[str, Any]]:
        for _ in range(max_retries):
            resp = await self._client.get(f"/repos/{owner}/{repo}/stats/commit_activity")
            if resp.status_code == 202:
                await asyncio.sleep(2)
                continue
            if resp.status_code != 200:
                return []
            return resp.json() or []
        return []

    async def list_issues(
            self,
            owner: str,
            repo: str,
            state: str = "open",
            per_page: int = 10,
            page: int = 1,
            sort: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params = {"state": state, "per_page": per_page, "page": page}
        if sort:
            params["sort"] = sort
        resp = await self._request("GET", f"/repos/{owner}/{repo}/issues", params=params, allow_404=True)
        return resp.json() if resp else []

    async def get_sbom(self, owner: str, repo: str) -> Optional[Dict[str, Any]]:
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/dependency-graph/sbom", allow_404=True
        )
        return resp.json() if resp else None

    async def list_stargazers_page(
            self, owner: str, repo: str, page: int = 1, per_page: int = 100
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """
        返回 (本页数据, 分页信息)。分页信息里 pages["last"] 是最后一页页码，
        上层算法（比如算最近 N 天新增星数）需要这个来决定从哪页开始往前翻。
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/stargazers",
            params={"per_page": per_page, "page": page},
            headers={"Accept": "application/vnd.github.star+json"},
            allow_404=True
        )
        if not resp:
            return [], {}
        pages = self._parse_link_header(resp.headers.get("Link"))
        return resp.json(), pages

    async def get_latest_release(self, owner: str, repo: str) -> Optional[Dict[str, Any]]:
        resp = await self._request("GET", f"/repos/{owner}/{repo}/releases/latest", allow_404=True)
        return resp.json() if resp else None

    async def search_repositories(
            self,
            query: str,
            sort: Optional[str] = None,
            order: str = "desc",
            per_page: int = 100,
            page: int = 1,
    ) -> Dict[str, Any]:
        params = {"q": query, "order": order, "per_page": per_page, "page": page}
        if sort:
            params["sort"] = sort
        resp = await self._request("GET", "/search/repositories", params=params)
        return resp.json()


    @classmethod
    def parse_github_url(cls, url: str) -> tuple:
        url = url.strip()
        url = url.replace("https://", "").replace("http://", "")
        url = url.rstrip("/")
        if "github.com/" in url:
            parts = url.split("github.com/")[1].split("/")
        else:
            parts = url.split("/")
        if len(parts) < 2:
            raise ValueError(f"无效的 GitHub URL: {url}")

        owner = parts[0]
        repo = parts[1]
        return owner, repo