import asyncio
import json
from datetime import datetime
from html import escape
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from app.bot.renderers.base import BaseProjectRenderer
from app.config import settings
from app.database.models import ContentItem, Summary
from app.logger import AppLogger
from app.services.news_service import NewsService

logger = AppLogger.get(__name__)

REVIEW_APPROVE = "review_approve"
REVIEW_REJECT = "review_reject"
REVIEW_EDIT = "review_edit"

#: (chat_id, message_id) -> item_id，识别「管理员直接编辑卡片消息」的同步目标
card_registry: dict[tuple[int, int], int] = {}


def _review_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 通过并推送", callback_data=f"{REVIEW_APPROVE}:{item_id}"),
            InlineKeyboardButton(text="❌ 拒绝", callback_data=f"{REVIEW_REJECT}:{item_id}"),
            InlineKeyboardButton(text="✏️ 编辑", callback_data=f"{REVIEW_EDIT}:{item_id}"),
        ]
    ])


def parse_edit_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """解析管理员发来的修改稿。

    约定：`一句话` 与 `正文` 用单独一行 `---` 分隔；
    不带分隔符时视为只修改正文（one_liner 保持不变）。
    返回 (one_liner | None, summary | None)，两者至少一个非 None。
    """
    text = (text or "").strip()
    if not text:
        return None, None
    lines = text.splitlines()
    sep_idx = next((i for i, l in enumerate(lines) if l.strip() == "---"), -1)
    if sep_idx > 0:
        one_liner = "\n".join(lines[:sep_idx]).strip() or None
        summary = "\n".join(lines[sep_idx + 1:]).strip() or None
        return one_liner, summary
    if sep_idx == 0:  # 以 --- 开头：只改正文
        return None, "\n".join(lines[1:]).strip() or None
    return None, text


def _format_body(text: str) -> str:
    """正文按段落拆分，每段以 emoji 引导开头，替代全角空格首行缩进。"""
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    return "\n\n".join(f"🔹 {p}" for p in paras)


def _parse_card_edit(text: str) -> tuple[Optional[str], Optional[str]]:
    """从编辑后的卡片纯文本反解出 (one_liner, summary)。

    约定：📌 行 = 一句话简介；🔹 段 = 正文段落（按出现顺序拼接）。
    标题/来源/摘录等行忽略（标题与 URL 以库内记录为准）。
    """
    one_liner_parts: list[str] = []
    body_parts: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("📌"):
            one_liner_parts.append(s[1:].strip())
        elif s.startswith("🔹"):
            body_parts.append(s[1:].strip())
    one_liner = "\n".join(one_liner_parts).strip() or None
    summary = "\n\n".join(body_parts).strip() or None
    return one_liner, summary


TELEGRAM_CAPTION_LIMIT = 1024


async def _send_public_card(bot: Bot, chat_id: int, item: ContentItem) -> None:
    """推公开频道：有配图且卡片文字能塞进 caption(1024字符) 就发大图卡片，
    否则退回纯文字（避免截断破坏 HTML 标签）。图片抓取自第三方 URL，
    不保证一直可用，send_photo 失败一律退回文字消息。"""
    text = _render_review_card(item, for_review=False)
    image_url = (item.raw_data or {}).get("image_url") if item.raw_data else None

    if image_url and len(text) <= TELEGRAM_CAPTION_LIMIT:
        try:
            await bot.send_photo(chat_id, photo=image_url, caption=text, parse_mode="HTML")
            return
        except Exception as e:
            logger.warning(f"配图推送失败，退回纯文字 item={item.id}: {e}")

    await bot.send_message(
        chat_id, text, parse_mode="HTML", disable_web_page_preview=False,
    )


def _render_review_card(item: ContentItem, for_review: bool = True) -> str:
    title = escape(item.title or "")
    url = item.url or ""
    title_link = f'<a href="{escape(url)}">{title}</a>' if url else title
    src = NewsService().get_source_label(item.source) if item.source else "📰 新闻"

    if for_review:
        cat = escape(item.category or "ai")
        # Telegram 无 heading，用「加粗标题 + 分隔线」模拟 h2 层级
        divider = "─" * 14
        header = f"🔍 <b>待审查新闻</b> · {src} · {cat}"
        lines = [
            header,
            "",
            f"📰 <b>{title_link}</b>",
            f"<i>{divider}</i>",
            "",
        ]
        if item.summary:
            s = item.summary
            # 📌 / 🔹 前缀是硬约定：管理员在频道里直接编辑本卡片时，
            # _parse_card_edit() 靠这两个标记反解出编辑内容，改动这两行的
            # 格式会破坏「直接编辑卡片」功能（见 review_handler.py）。
            if getattr(s, "one_liner", None):
                lines.append(f"📌 {escape(str(s.one_liner))}")
            if getattr(s, "summary", None):
                body = escape(str(s.summary).strip())
                if len(body) > 400:
                    body = body[:400] + "…"
                lines.append("")
                lines.append(_format_body(body))
            # Q3 闸门：摘要没有原文摘录支撑时标红提示，仅审查卡片显示，
            # 正式推送到公开频道的卡片不带这条警告。
            quote_list = _coerce_quotes(getattr(s, "source_quotes", None))
            if not _quote_coverage_ok(str(s.summary or ""), quote_list):
                lines.append("")
                lines.append("⚠️ <b>准确性待核</b>：摘要无原文摘录支撑，请谨慎通过")
        return "\n".join(lines)

    # 公开频道：杂志摘要风。这份不会被反解编辑，不需要 📌/🔹 标记，
    # 摘要整体放进一个引用块，读起来像文章摘要而不是列表条目。
    cat_label = escape(NewsService().get_category_label(item.category or "ai"))
    metrics = item.metrics or {}
    time_label = None
    published_at = metrics.get("published_at")
    if published_at:
        try:
            dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
            time_label = BaseProjectRenderer._time_ago(dt)
        except Exception:
            pass
    meta_parts = [src, cat_label]
    if time_label:
        meta_parts.append(f"🕐 {time_label}")
    lines = [" · ".join(meta_parts), "", f"<b>{title_link}</b>"]

    if item.summary:
        s = item.summary
        if s.score and s.score > 0:
            bar = BaseProjectRenderer._score_bar(s.score)
            score_line = f"{bar} <b>{s.score:.1f}</b>/10"
            maturity = getattr(s, "maturity_level", None)
            if maturity:
                score_line += f" · {escape(str(maturity).strip())}"
            lines.append("")
            lines.append(score_line)

        quote_segments = []
        if getattr(s, "one_liner", None):
            quote_segments.append(escape(str(s.one_liner).strip()))
        if getattr(s, "summary", None):
            body = escape(str(s.summary).strip())
            if len(body) > 400:
                body = body[:400] + "…"
            quote_segments.append(body)
        if quote_segments:
            quote_text = "\n\n".join(quote_segments)
            lines.append("")
            lines.append(f"<blockquote>❝ {quote_text} ❞</blockquote>")

        tags = s.get_tags_list()[:3] if hasattr(s, "get_tags_list") else []
        if tags:
            tag_str = " ".join(f"#{escape(str(t).replace(' ', '_'))}" for t in tags)
            lines.append("")
            lines.append(f"🏷 {tag_str}")

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

    def update_summary(self, item_id: int,
                       one_liner: Optional[str] = None,
                       summary: Optional[str] = None) -> bool:
        """管理员修改稿落库：更新该新闻的 Summary（不存在则创建）。"""
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            s = db.query(Summary).filter(Summary.project_id == item_id).first()
            if not s:
                s = Summary(
                    project_id=item_id,
                    one_liner=one_liner or "",
                    summary=summary or "",
                    score=0.0,
                    model_used="manual_edit",
                    processing_status="success",
                )
                db.add(s)
            else:
                if one_liner is not None:
                    s.one_liner = one_liner
                if summary is not None:
                    s.summary = summary
            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logger.error(f"更新摘要失败 item={item_id}: {e}")
            return False
        finally:
            db.close()

    async def send_for_review(self, bot: Bot) -> int:
        """推送待审查新闻。优先推送到审查频道（review_chat_id，bot 需为该频道
        管理员），未配置时退回各审查管理员私聊（review_admin_ids）。
        返回成功送审条数。"""
        items = self.news.get_unreviewed_news(limit=50)
        if settings.review_chat_id:
            targets = [settings.review_chat_id]
            dest = f"审查频道 {settings.review_chat_id}"
        elif settings.review_admin_ids:
            targets = list(settings.review_admin_ids)
            dest = "管理员私聊"
        else:
            logger.warning("review_chat_id / review_admin_ids 均未配置，跳过审查推送")
            return 0
        sent = 0
        for item in items:
            delivered = False
            for target in targets:
                try:
                    msg = await bot.send_message(
                        target,
                        _render_review_card(item),
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                        reply_markup=_review_keyboard(item.id),
                    )
                    card_registry[(target, msg.message_id)] = item.id
                    delivered = True
                except TelegramRetryAfter as e:
                    logger.warning(
                        f"触发限流 item={item.id} target={target}，等待 {e.retry_after}s 后重试"
                    )
                    await asyncio.sleep(e.retry_after + 1)
                    try:
                        msg = await bot.send_message(
                            target,
                            _render_review_card(item),
                            parse_mode="HTML",
                            disable_web_page_preview=False,
                            reply_markup=_review_keyboard(item.id),
                        )
                        card_registry[(target, msg.message_id)] = item.id
                        delivered = True
                    except Exception as e2:
                        logger.error(f"送审失败 item={item.id} target={target}: {e2}")
                except Exception as e:
                    logger.error(f"送审失败 item={item.id} target={target}: {e}")
            if delivered:
                self.news.set_review_status(item.id, "pending")
                sent += 1
                # 频道/群组限制约 20 条/分钟，间隔放宽到 3s 防止再次触发限流
                await asyncio.sleep(3)
        logger.info(f"🔍 送审 {sent} 条新闻到{dest}")
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
                await _send_public_card(bot, settings.public_channel_id, item)
            except Exception as e:
                logger.error(f"转发公开频道失败 item={item_id}: {e}")
                return False
        self.news.set_review_status(item_id, "approved")
        if query_message:
            # 通过即销毁卡片：正式稿已在公开频道，审查卡片不再保留。
            try:
                await bot.delete_message(
                    query_message.chat.id, query_message.message_id
                )
            except Exception as e:
                logger.warning(f"删除已通过卡片失败 item={item_id}: {e}，退回标记文本")
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
            # 拒绝即销毁卡片：从审查频道/私聊删除，避免堆积影响查看。
            try:
                await bot.delete_message(
                    query_message.chat.id, query_message.message_id
                )
            except Exception as e:
                logger.warning(f"删除已拒绝卡片失败 item={item_id}: {e}，退回标记文本")
                try:
                    await query_message.edit_text(
                        query_message.text + "\n\n❌ 已拒绝",
                        parse_mode="HTML", reply_markup=None,
                    )
                except Exception:
                    pass
        return ok
