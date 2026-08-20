import asyncio
import json
from html import escape

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from app.config import settings
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
    src = NewsService().get_source_label(item.source) if item.source else "📰 新闻"
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
        quote_list = _coerce_quotes(quotes)
        if quote_list:
            for q in quote_list[:2]:
                lines.append(f"❝ {escape(str(q).strip())}")
        # Q3 闸门：摘要必须有原文摘录支撑，否则向审查员标红
        if not _quote_coverage_ok(str(s.summary or ""), quote_list):
            lines.append("⚠️ <b>准确性待核</b>：摘要无原文摘录支撑，请谨慎通过")
    return "\n".join(lines)


def _coerce_quotes(quotes) -> list:
    """source_quotes 可能是 list / JSON 字符串 / None，统一成 list[str]。"""
    if not quotes:
        return []
    if isinstance(quotes, str):
        try:
            quotes = json.loads(quotes)
        except (json.JSONDecodeError, TypeError):
            return []
    return quotes if isinstance(quotes, list) else []


def _quote_coverage_ok(summary_text: str, quotes: list) -> bool:
    """Q3 硬约束的轻量校验：有摘录且摘要里至少有一句能被某条摘录覆盖。

    简化判定：存在非空摘录即视为有支撑；若摘录为空则视为不支持。
    完整判定依赖人工审查，这里只做「有无摘录」的闸门提示。
    """
    if not summary_text or not summary_text.strip():
        return True
    return bool([q for q in quotes if isinstance(q, str) and q.strip()])


class ReviewService:
    def __init__(self):
        self.news = NewsService()

    async def send_for_review(self, bot: Bot) -> int:
        """推送待审查新闻到各审查管理员私聊。返回成功送审条数。"""
        admin_ids = settings.review_admin_ids
        if not admin_ids:
            logger.warning("review_admin_ids 未配置，跳过审查推送")
            return 0
        items = self.news.get_unreviewed_news(limit=50)
        sent = 0
        for item in items:
            delivered = False
            for admin_id in admin_ids:
                try:
                    await bot.send_message(
                        admin_id,
                        _render_review_card(item),
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                        reply_markup=_review_keyboard(item.id),
                    )
                    delivered = True
                except Exception as e:
                    logger.error(f"送审失败 item={item.id} admin={admin_id}: {e}")
            if delivered:
                self.news.set_review_status(item.id, "pending")
                sent += 1
                await asyncio.sleep(0.2)
        logger.info(f"🔍 送审 {sent} 条新闻到管理员私聊")
        return sent

    async def approve(self, bot: Bot, item_id: int, query_message=None) -> bool:
        item = self.news.get_by_id(item_id)
        if not item:
            return False
        if item.review_status != "pending":
            logger.warning(f"拒绝重复通过 item={item_id} status={item.review_status}")
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
        item = self.news.get_by_id(item_id)
        if not item:
            return False
        if item.review_status != "pending":
            logger.warning(f"拒绝重复操作 item={item_id} status={item.review_status}")
            return False
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
