"""Phase 3B Q4 review gate: ReviewService state transitions (mocked bot + DB)."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from app.services.review_service import ReviewService
from app.database.models import ContentItem
from app.config import settings


def _fake_item(item_id=10, source="news", processed=1, review_status=None):
    item = MagicMock(spec=ContentItem)
    item.id = item_id
    item.source = source
    item.title = "Test News"
    item.url = "https://example.com/n"
    item.category = "ai"
    item.summary = None
    return item


@pytest.mark.asyncio
async def test_send_for_review_marks_pending(monkeypatch):
    monkeypatch.setattr(settings, "review_chat_id", 999)
    svc = ReviewService()
    item = _fake_item()
    monkeypatch.setattr(svc.news, "get_unreviewed_news", lambda limit=50: [item])
    monkeypatch.setattr(svc.news, "set_review_status", MagicMock())

    bot = MagicMock()
    bot.send_message = AsyncMock()

    sent = await svc.send_for_review(bot)
    assert sent == 1
    bot.send_message.assert_awaited_once()
    svc.news.set_review_status.assert_called_once_with(10, "pending")


@pytest.mark.asyncio
async def test_approve_forwards_and_marks_approved(monkeypatch):
    monkeypatch.setattr(settings, "public_channel_id", 888)
    svc = ReviewService()
    item = _fake_item()
    monkeypatch.setattr(svc.news, "get_by_id", lambda i: item)
    monkeypatch.setattr(svc.news, "set_review_status", MagicMock())

    bot = MagicMock()
    bot.send_message = AsyncMock()
    msg = MagicMock()
    msg.text = "card"
    msg.edit_text = AsyncMock()

    ok = await svc.approve(bot, 10, query_message=msg)
    assert ok is True
    bot.send_message.assert_awaited_once()  # forwarded to public channel
    svc.news.set_review_status.assert_called_once_with(10, "approved")
    msg.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_reject_marks_rejected(monkeypatch):
    svc = ReviewService()
    monkeypatch.setattr(svc.news, "set_review_status", MagicMock(return_value=True))

    bot = MagicMock()
    msg = MagicMock()
    msg.text = "card"
    msg.edit_text = AsyncMock()

    ok = await svc.reject(bot, 10, query_message=msg)
    assert ok is True
    svc.news.set_review_status.assert_called_once_with(10, "rejected")
    msg.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_no_channel_skips_forward(monkeypatch):
    monkeypatch.setattr(settings, "public_channel_id", 0)
    svc = ReviewService()
    item = _fake_item()
    monkeypatch.setattr(svc.news, "get_by_id", lambda i: item)
    monkeypatch.setattr(svc.news, "set_review_status", MagicMock())

    bot = MagicMock()
    bot.send_message = AsyncMock()
    msg = MagicMock()
    msg.text = "card"
    msg.edit_text = AsyncMock()

    ok = await svc.approve(bot, 10, query_message=msg)
    assert ok is True
    bot.send_message.assert_not_awaited()  # no channel configured
    svc.news.set_review_status.assert_called_once_with(10, "approved")
