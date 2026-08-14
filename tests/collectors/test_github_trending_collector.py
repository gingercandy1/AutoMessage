import pytest
from unittest.mock import AsyncMock, patch

import httpx

from app.collectors.github_collector import GitHubTrendingCollector, GithubConfig


class TestGitHubTrendingCollectorConfigs:
    """GitHubTrendingCollector 之前 configs() 返回 [] 导致调度跳过（死代码）。"""

    def test_configs_non_empty(self):
        """configs() 必须返回非空，否则调度循环 for config in []: 跳过。"""
        configs = GitHubTrendingCollector().configs()
        assert isinstance(configs, list)
        assert len(configs) > 0

    def test_configs_each_has_language(self):
        """每个配置都是 GithubConfig 且带 language 属性（None=全语言 或 具体语言）。"""
        configs = GitHubTrendingCollector().configs()
        for cfg in configs:
            assert isinstance(cfg, GithubConfig)
            assert hasattr(cfg, "language")

    def test_configs_includes_all_and_languages(self):
        """应包含『全部语言』(language=None) 与各语言配置，覆盖 GitHubCollector 的语言集。"""
        configs = GitHubTrendingCollector().configs()
        languages = {c.language for c in configs}
        assert None in languages  # 首页 trending
        assert "python" in languages


class _FakeResponse:
    def __init__(self, text: str, raise_http_error: bool = False):
        self._text = text
        self._raise_http_error = raise_http_error

    @property
    def text(self) -> str:
        return self._text

    def raise_for_status(self):
        if self._raise_http_error:
            raise httpx.HTTPError("boom")


class _FakeClient:
    """替代 httpx.AsyncClient 的轻量 fake（稳定支持 async with / await get）。"""

    def __init__(self, response=None, get_raises=None):
        self._response = response
        self._get_raises = get_raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        if self._get_raises is not None:
            raise self._get_raises
        return self._response


class TestGitHubTrendingCollectorFetch:
    """fetch 必须返回保存数量（int），否则调度里 total += count 因 None 崩溃。"""

    @pytest.mark.asyncio
    async def test_fetch_parses_and_returns_save_count(self):
        collector = GitHubTrendingCollector()
        collector.client.get_repo = AsyncMock(return_value={
            "stargazers_count": 100,
            "forks_count": 10,
            "watchers_count": 5,
            "license": None,
            "created_at": None,
            "updated_at": None,
        })

        html = """
        <article class="Box-row">
          <h2><a href="/owner/repo">owner/repo</a></h2>
          <p>Some description</p>
        </article>
        """
        client = _FakeClient(response=_FakeResponse(text=html))

        with patch("app.collectors.github_collector.httpx.AsyncClient", return_value=client):
            with patch.object(collector, "save", new=AsyncMock(return_value=3)) as mock_save:
                result = await collector.fetch(GithubConfig(language="python"))

        mock_save.assert_awaited_once()
        assert result == 3  # 调度依赖此返回值累加

    @pytest.mark.asyncio
    async def test_fetch_handles_request_error(self):
        """请求失败时 fetch 返回 0（不抛异常中断调度）。"""
        collector = GitHubTrendingCollector()
        client = _FakeClient(get_raises=Exception("network down"))

        with patch("app.collectors.github_collector.httpx.AsyncClient", return_value=client):
            result = await collector.fetch(GithubConfig(language="python"))

        assert result == 0
