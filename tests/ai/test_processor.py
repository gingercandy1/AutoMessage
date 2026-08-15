import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.ai.processor import BatchSummary
from app.collectors.base import BaseAnalyzer
from app.database.models import ContentItem, Summary


class TestBatchSummaryCostPersistence:
    """测试 BatchSummary 持久化 token_cost"""

    @pytest.fixture
    def mock_analyzer(self):
        analyzer = MagicMock(spec=BaseAnalyzer)
        analyzer.build_context = AsyncMock(return_value="test prompt")
        return analyzer

    @pytest.fixture
    def mock_project(self):
        project = MagicMock(spec=ContentItem)
        project.id = 1
        project.title = "test-repo"
        project.url = "https://github.com/test/repo"
        project.source = "github"
        project.processed = 0
        return project

    @pytest.fixture
    def mock_db_session(self):
        with patch("app.ai.processor.SessionLocal") as mock_session_local:
            mock_db = MagicMock()
            mock_session_local.return_value = mock_db
            yield mock_db

    @pytest.mark.asyncio
    async def test_process_single_persists_token_cost(self, mock_analyzer, mock_project, mock_db_session):
        """_process_single 应将 token_cost 保存到 Summary"""
        # Arrange
        batch = BatchSummary(analyzer=mock_analyzer, source="github")
        
        mock_summarizer = AsyncMock()
        mock_summarizer.summarize = AsyncMock(return_value={
            "one_liner": "test",
            "summary": "test summary",
            "use_cases": "testing",
            "pros": ["pro1"],
            "cons": ["con1"],
            "score": 8.5,
            "tags": ["tag1"],
            "install_step": "pip install",
            "maturity_level": "稳定可用",
            "model": "openai/gpt-4o-mini",
            "token_cost": 0.00123,
            "provider": "openrouter"
        })

        # Mock query for summarized_ids
        mock_query = mock_db_session.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = [mock_project]

        # Act
        result = await batch._process_single(mock_summarizer, mock_project, AsyncMock())

        # Assert
        assert result["success"] is True
        # Verify Summary was created with token_cost
        mock_db_session.merge.assert_called_once()
        saved_summary = mock_db_session.merge.call_args[0][0]
        assert isinstance(saved_summary, Summary)
        assert saved_summary.token_cost == 0.00123
        assert saved_summary.model_used == "openai/gpt-4o-mini"