import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.ai.summarizer import ProjectSummarizer


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
        """JSON 解析失败时应重试一次并附加更严格指令"""
        summarizer = ProjectSummarizer(provider="openrouter")
        
        # First response: malformed JSON (no code block)
        # Second response: valid JSON
        mock_response_1 = MagicMock()
        mock_response_1.json.return_value = {
            "choices": [{"message": {"content": "not valid json {{" }}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}
        }
        
        mock_response_2 = MagicMock()
        mock_response_2.json.return_value = {
            "choices": [{"message": {"content": '{"one_liner": "test", "summary": "test", "pros": [], "cons": [], "score": 5, "tags": [], "install_step": "", "maturity_level": "早期项目"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}
        }
        
        mock_httpx_client.post.side_effect = [mock_response_1, mock_response_2]

        async with summarizer:
            result = await summarizer.summarize("test prompt")

        # Should succeed on retry
        assert "error" not in result
        assert result["one_liner"] == "test"
        # Should have called post twice (original + retry)
        assert mock_httpx_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_parse_json_failure_logged(self, mock_httpx_client, caplog):
        """JSON 解析最终失败时应记录原始响应"""
        summarizer = ProjectSummarizer(provider="openrouter")
        
        # Both responses malformed
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "completely invalid {{" }}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}
        }
        mock_httpx_client.post.return_value = mock_response

        async with summarizer:
            result = await summarizer.summarize("test prompt")

        # Should return error with parse_failed flag
        assert "error" in result
        assert result.get("parse_failed") is True
        # Raw content should be logged (check caplog)
        assert any("JSON 解析失败" in record.message for record in caplog.records)