import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai.summarizer import ProjectSummarizer, ProviderCandidate


VALID_CONTENT = (
    '{"one_liner": "test", "summary": "test", "pros": [], "cons": [], '
    '"score": 5, "tags": [], "install_step": "", "maturity_level": "早期项目"}'
)


class TestProjectSummarizerCostTracking:
    """测试 ProjectSummarizer 返回 token_cost"""

    @pytest.fixture
    def mock_httpx_client(self):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client
            yield mock_client

    @pytest.mark.asyncio
    async def test_summarize_openrouter_returns_token_cost(self, mock_httpx_client):
        summarizer = ProjectSummarizer(
            candidates=[ProviderCandidate("openrouter", "openai/gpt-4o",
                                          "https://openrouter.ai/api/v1", "sk")]
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": VALID_CONTENT}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
        mock_httpx_client.post.return_value = mock_response

        async with summarizer:
            result = await summarizer.summarize("test prompt")

        assert "token_cost" in result
        assert isinstance(result["token_cost"], (int, float))
        assert result["token_cost"] > 0
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_summarize_openai_returns_token_cost(self, mock_httpx_client):
        summarizer = ProjectSummarizer(
            candidates=[ProviderCandidate("openai", "gpt-4o",
                                          "https://api.openai.com/v1", "sk")]
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": VALID_CONTENT}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
        }
        mock_httpx_client.post.return_value = mock_response

        async with summarizer:
            result = await summarizer.summarize("test prompt")

        assert "token_cost" in result
        assert isinstance(result["token_cost"], (int, float))
        assert result["token_cost"] > 0
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_summarize_ollama_returns_zero_cost(self, mock_httpx_client):
        summarizer = ProjectSummarizer(
            candidates=[ProviderCandidate("ollama", "qwen2.5:14b",
                                          "http://localhost:11434", None)]
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message": {"content": VALID_CONTENT}}
        mock_httpx_client.post.return_value = mock_response

        async with summarizer:
            result = await summarizer.summarize("test prompt")

        assert "token_cost" in result
        assert result["token_cost"] == 0
