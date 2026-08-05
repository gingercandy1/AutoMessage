import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.ai.summarizer import ProjectSummarizer


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
        """OpenRouter 返回 usage，summarize 应包含 token_cost"""
        # Arrange
        summarizer = ProjectSummarizer(provider="openrouter")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"one_liner": "test", "summary": "test", "pros": [], "cons": [], "score": 5, "tags": [], "install_step": "", "maturity_level": "早期项目"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        }
        mock_httpx_client.post.return_value = mock_response

        # Act
        async with summarizer:
            result = await summarizer.summarize("test prompt")

        # Assert
        assert "token_cost" in result
        assert isinstance(result["token_cost"], (int, float))
        assert result["token_cost"] > 0
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_summarize_openai_returns_token_cost(self, mock_httpx_client):
        """OpenAI 返回 usage，summarize 应包含 token_cost"""
        summarizer = ProjectSummarizer(provider="openai")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"one_liner": "test", "summary": "test", "pros": [], "cons": [], "score": 5, "tags": [], "install_step": "", "maturity_level": "早期项目"}'}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}
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
        """Ollama 无成本，token_cost 应为 0"""
        summarizer = ProjectSummarizer(provider="ollama")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": '{"one_liner": "test", "summary": "test", "pros": [], "cons": [], "score": 5, "tags": [], "install_step": "", "maturity_level": "早期项目"}'}
        }
        mock_httpx_client.post.return_value = mock_response

        async with summarizer:
            result = await summarizer.summarize("test prompt")

        assert "token_cost" in result
        assert result["token_cost"] == 0