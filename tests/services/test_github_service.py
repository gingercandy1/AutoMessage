import pytest
from unittest.mock import MagicMock, patch
from app.services.github_service import GithubService
from app.database.models import ContentItem

class TestGithubService:
    @pytest.fixture
    def mock_session(self):
        return MagicMock()

    @patch("app.services.github_service.SessionLocal")
    def test_get_total_hot(self, mock_session_local):
        # Arrange
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        service = GithubService()
        
        # Mock the query chain
        mock_query = mock_db.query.return_value
        mock_query.options.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = [MagicMock(spec=ContentItem)]

        # Act
        result = service.get_total_hot(limit=5)

        # Assert
        assert len(result) == 1
        assert isinstance(result[0], ContentItem)
        mock_db.close.assert_called_once()

    @patch("app.services.github_service.SessionLocal")
    def test_search_project_not_found(self, mock_session_local):
        # Arrange
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        service = GithubService()
        
        mock_query = mock_db.query.return_value
        mock_query.options.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.first.return_value = None

        # Act
        result = service.search_project("non-existent-repo")

        # Assert
        assert result is None
        mock_db.close.assert_called_once()
