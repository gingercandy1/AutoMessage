import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.collectors.github_collector import GitHubCollector
from app.api.github_client import GitHubRateLimitError


class TestGitHubCollectorRateLimit:
    """测试 GitHubCollector 速率限制重试"""

    @pytest.fixture
    def mock_client(self):
        client = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_get_star_growth_retries_on_rate_limit(self, mock_client):
        """get_star_growth 遇到速率限制应重试"""
        collector = GitHubCollector(client=mock_client)
        
        # First call (initial page) raises rate limit, second succeeds, third (last page) succeeds
        mock_client.list_stargazers_page.side_effect = [
            GitHubRateLimitError("Rate limited", reset_at=0),  # First call fails
            ([{"starred_at": "2024-01-15T10:00:00Z"}], {"last": 1}),  # Second call (retry initial) succeeds
            ([{"starred_at": "2024-01-15T10:00:00Z"}], {"last": 1})  # Third call (last page) succeeds
        ]

        result = await collector.get_star_growth("owner", "repo", days=7, max_pages=1)
        
        assert "recent_stars" in result
        assert result["recent_stars"] == 1
        assert mock_client.list_stargazers_page.call_count == 3

    @pytest.mark.asyncio
    async def test_get_star_growth_max_retries_exceeded(self, mock_client):
        """超过最大重试次数后应抛出异常"""
        collector = GitHubCollector(client=mock_client)
        
        # Always raise rate limit
        mock_client.list_stargazers_page.side_effect = GitHubRateLimitError("Rate limited", reset_at=0)

        with pytest.raises(GitHubRateLimitError):
            await collector.get_star_growth("owner", "repo", days=7, max_pages=1)
        
        # Should retry 3 times by default
        assert mock_client.list_stargazers_page.call_count >= 3