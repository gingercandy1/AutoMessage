import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai.summarizer import ProjectSummarizer, ProviderCandidate


VALID_CONTENT = (
    '{"one_liner": "test", "summary": "test", "pros": [], "cons": [], '
    '"score": 5, "tags": [], "install_step": "", "maturity_level": "早期项目"}'
)


class TestProjectSummarizerJsonRetry:
    """测试 JSON 解析失败重试机制"""

    @pytest.fixture
    def mock_httpx_client(self):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client
            yield mock_client

    @pytest.mark.asyncio
    async def test_parse_json_retry_on_failure(self, mock_httpx_client):
        summarizer = ProjectSummarizer(
            candidates=[ProviderCandidate("openrouter", "openai/gpt-4o",
                                          "https://openrouter.ai/api/v1", "sk")]
        )

        mock_response_1 = MagicMock()
        mock_response_1.status_code = 200
        mock_response_1.json.return_value = {
            "choices": [{"message": {"content": "not valid json {{"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        mock_response_2 = MagicMock()
        mock_response_2.status_code = 200
        mock_response_2.json.return_value = {
            "choices": [{"message": {"content": VALID_CONTENT}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        mock_httpx_client.post.side_effect = [mock_response_1, mock_response_2]

        async with summarizer:
            result = await summarizer.summarize("test prompt")

        assert "error" not in result
        assert result["one_liner"] == "test"
        assert mock_httpx_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_parse_json_failure_logged(self, mock_httpx_client, caplog):
        summarizer = ProjectSummarizer(
            candidates=[ProviderCandidate("openrouter", "openai/gpt-4o",
                                          "https://openrouter.ai/api/v1", "sk")]
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "completely invalid {{"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        mock_httpx_client.post.return_value = mock_response

        async with summarizer:
            result = await summarizer.summarize("test prompt")

        # 解析最终失败：返回候选不可用错误，且原始响应被记录
        assert "error" in result
        assert any("JSON 解析失败" in record.message for record in caplog.records)
