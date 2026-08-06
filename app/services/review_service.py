"""Q4 人工审查闸门（ADR-0001 §8）。

流程：
  1. scheduler 触发 send_for_review()：把 AI 已摘要(processed=1)、未送审(review_status IS NULL)
     的新闻推到 settings.review_chat_id，每条附 ✅/❌ inline 按钮。
  2. 管理员在审查群组点 ✅ → 转发到公开频道 settings.public_channel_id，标记 approved；
     点 ❌ → 标记 rejected，编辑原消息注明已拒绝。

review_status 字段：NULL=未送审, pending=审查中, approved=通过, rejected=拒绝。
"""
from __future__ import annotations

import asyncio
import json
from html import escape
from typing import List

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from app.config import settings
from app.database import SessionLocal
from app.database.models import ContentItem
from app.logger import AppLogger
from app.services.news_service import NewsService

logger = AppLogger.get(__name__)

REVIEW_APPROVE = "review_approve"
REVIEW_REJECT = "review_reject"


def _review_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 通过并推送", callback_data=f"{REVIEW_APPROVE}:{item_id}"),
            InlineKeyboardButton(text="❌ 拒绝", callback_data=f"{REVIEW_REJECT}:{item_id}"),
        ]
    ])


def _render_review_card(item: ContentItem) -> str:
    title = escape(item.title or "")
    url = item.url or ""
    title_link = f'<a href="{url}">{title}</a>' if url else title
    src = "🟠 HN" if item.source == "hacker_news" else "📰 新闻"
    cat = escape(item.category or "ai")
    lines = [f"🔍 <b>待审查新闻</b> · {src} · {cat}", "", f"<b>{title_link}</b>"]
    if item.summary:
        s = item.summary
        if getattr(s, "one_liner", None):
            lines.append(escape(str(s.one_liner)))
        if getattr(s, "summary", None):
            body = escape(str(s.summary).strip())
            if len(body) > 400:
                body = body[:400] + "…"
            lines.append(body)
        # 原文摘录（Q3 准确性核对）
        quotes = getattr(s, "source_quotes", None)
        if quotes:
            if isinstance(quotes, str):
                try:
                    quotes = json.loads(quotes)
                except (json.JSONDecodeError, TypeError):
                    quotes = []
            if isinstance(quotes, list):
                for q in quotes[:2]:
                    lines.append(f"❝ {escape(str(q).strip())}")
    return "\n".join(lines)


class ReviewService:
    def __init__(self):
        self.news = NewsService()

    async def send_for_review(self, bot: Bot) -> int:
        """推送待审查新闻到审查群组。返回成功送审条数。"""
        if not settings.review_chat_id:
            logger.warning("review_chat_id 未配置，跳过审查推送")
            return 0
        items = self.news.get_unreviewed_news(limit=50)
        sent = 0
        for item in items:
            try:
                await bot.send_message(
                    settings.review_chat_id,
                    _render_review_card(item),
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                    reply_markup=_review_keyboard(item.id),
                )
                self.news.set_review_status(item.id, "pending")
                sent += 1
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"送审失败 item={item.id}: {e}")
        logger.info(f"🔍 送审 {sent} 条新闻到审查群组")
        return sent

    async def approve(self, bot: Bot, item_id: int, query_message=None) -> bool:
        item = self.news.get_by_id(item_id)
        if not item:
            return False
        if settings.public_channel_id:
            try:
                await bot.send_message(
                    settings.public_channel_id,
                    _render_review_card(item),
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
            except Exception as e:
                logger.error(f"转发公开频道失败 item={item_id}: {e}")
                return False
        self.news.set_review_status(item_id, "approved")
        if query_message:
            try:
                await query_message.edit_text(
                    query_message.text + "\n\n✅ 已通过并推送",
                    parse_mode="HTML", reply_markup=None,
                )
            except Exception:
                pass
        return True

    async def reject(self, bot: Bot, item_id: int, query_message=None) -> bool:
        ok = self.news.set_review_status(item_id, "rejected")
        if ok and query_message:
            try:
                await query_message.edit_text(
                    query_message.text + "\n\n❌ 已拒绝",
                    parse_mode="HTML", reply_markup=None,
                )
            except Exception:
                pass
        return ok
