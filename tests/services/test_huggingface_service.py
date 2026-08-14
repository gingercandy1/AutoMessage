import pytest
from unittest.mock import MagicMock, patch
from app.services.huggingface_service import HuggingFaceService
from app.database.models import ContentItem

class TestHuggingFaceService:
    @patch("app.services.huggingface_service.SessionLocal")
    def test_get_latest_ranking_empty(self, mock_session_local):
        # Arrange
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        service = HuggingFaceService()
        
        # Mock _latest_fetched_at to return None (no data found)
        with patch.object(HuggingFaceService, '_latest_fetched_at', return_value=None):
            # Act
            result = service.get_latest_ranking(category_key="text-generation")

            # Assert
            assert result == []
            mock_db.close.assert_called_once()

    @patch("app.services.huggingface_service.SessionLocal")
    def test_get_all_categories(self, mock_session_local):
        # Arrange
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        service = HuggingFaceService()
        
        # Mock the query for distinct categories
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.distinct.return_value = mock_query
        mock_query.all.return_value = [("text-generation",), ("image-to-text",)]

        # Act
        result = service.get_all_categories()

        # Assert
        assert result == ["text-generation", "image-to-text"]
        mock_db.close.assert_called_once()

    @patch("app.services.huggingface_service.SessionLocal")
    def test_get_trend_returns_data(self, mock_session_local):
        # Arrange
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        service = HuggingFaceService()

        # Mocking the internal call to get_latest_ranking
        with patch.object(HuggingFaceService, 'get_latest_ranking', return_value=[MagicMock(spec=ContentItem)]):
            # Act
            result = service.get_trend(category_key="text-generation")

            # Assert: get_trend delegates to get_latest_ranking and returns its result.
            # (Session open/close lives inside get_latest_ranking, so it is NOT exercised
            # here — asserting mock_db.close would test the mocked method, not get_trend.)
            assert len(result) == 1
            assert isinstance(result[0], ContentItem)
