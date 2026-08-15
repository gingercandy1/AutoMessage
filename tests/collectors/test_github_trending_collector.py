import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.collectors.github_collector import GitHubTrendingCollector
from app.database.models import ContentItem


class TestGitHubTrendingCollectorFetch:
    @pytest.mark.asyncio
    async def test_fetch_parses_and_returns_save_count(self):
        collector = GitHubTrendingCollector()
        item = ContentItem(
            source="github", external_id="octocat/Hello-World", title="Hello-World",
            url="https://github.com/octocat/Hello-World", description="desc",
            author="octocat", metrics={"stars": 100}, raw_data={},
        )

        collector._parse = AsyncMock(return_value=[item])
        mock_save = AsyncMock(return_value=1)
        collector.save = mock_save

        with patch("httpx.AsyncClient") as mc:
            client = AsyncMock()
            mc.return_value.__aenter__.return_value = client
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "<html></html>"
            client.get.return_value = resp

            count = await collector.fetch()

        assert count == 1
        mock_save.assert_awaited_once()
