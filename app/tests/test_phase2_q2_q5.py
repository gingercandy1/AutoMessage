"""Phase 2 Q2/Q5: service query + subscription persistence (mocked DB)."""
import pytest
from unittest.mock import MagicMock, patch


def _mock_session_returns(rows):
    session = MagicMock()
    query_mock = MagicMock()
    query_mock.filter.return_value = query_mock
    query_mock.filter_by.return_value = query_mock
    query_mock.order_by.return_value = query_mock
    query_mock.limit.return_value = query_mock
    query_mock.all.return_value = rows
    session.query.return_value = query_mock
    return session


@pytest.mark.asyncio
async def test_hf_service_recently_updated_filters_by_last_modified(monkeypatch):
    from app.services.huggingface_service import HuggingFaceService
    from app.database.models import ContentItem

    fake_item = MagicMock(spec=ContentItem)
    fake_item.metrics = {"last_modified": "2024-06-01T00:00:00Z", "likes": 5, "downloads": 10}
    session = _mock_session_returns([fake_item])
    monkeypatch.setattr("app.services.huggingface_service.SessionLocal", lambda: session)

    svc = HuggingFaceService()
    result = svc.get_recently_updated(limit=10, days=30)
    assert len(result) == 1
    # order_by must have been applied on the query chain
    q = session.query.return_value
    order_by_calls = [m for m in q.method_calls if m[0] == "order_by"]
    assert order_by_calls, "expected order_by to be applied"



@pytest.mark.asyncio
async def test_subscribe_writes_subscription(monkeypatch):
    from app.bot import subscription_handler as sh

    session = MagicMock()
    # simulate "not yet subscribed" -> query returns None
    query_mock = MagicMock()
    query_mock.filter_by.return_value = query_mock
    query_mock.first.return_value = None
    session.query.return_value = query_mock
    monkeypatch.setattr(sh, "SessionLocal", lambda: session)

    changed = sh._set_subscription(12345, active=True)
    assert changed is True
    # a Subscription row should have been added
    assert session.add.called
    assert session.commit.called
