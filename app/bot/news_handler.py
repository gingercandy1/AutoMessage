import hashlib
from datetime import datetime, timezone
from html import escape
from typing import Any, Callable, Union

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

from app.database.models import ContentItem
from app.logger import AppLogger
from app.bot.search_state import set_pending, clear_pending, is_pending
from app.services.news_service import NewsService

logger = AppLogger.get(__name__)
router = Router()
PAGE_SIZE = 6

service = NewsService()

_fav_id_cache: dict[str, str] = {}


def _make_fav_id(item_id: int) -> str:
    short_id = hashlib.md5(str(item_id).encode()).hexdigest()[:10]
    _fav_id_cache[short_id] = str(item_id)
    return short_id


def _get_fav_id(short_id: str) -> str | None:
    return _fav_id_cache.get(short_id)


class NewsProjectRenderer:
    CATEGORY_ICONS = {
        "ai": "🤖",
        "llm": "🧠",
        "research": "📄",
        "tools": "🔧",
        "industry": "🏢",
    }

    SOURCE_ICONS = {
        "hacker_news": "🟠",
        "news": "📰",
    }

    @staticmethod
    def _category_icon(category: str | None) -> str:
        if not category:
            return "📰"
        return NewsProjectRenderer.CATEGORY_ICONS.get(category, "📰")

    @staticmethod
    def _source_icon(source: str | None) -> str:
        if not source:
            return "📰"
        return NewsProjectRenderer.SOURCE_ICONS.get(source, "📰")

    @staticmethod
    def _format_count(n: int | None) -> str:
        n = n or 0
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    @staticmethod
    def _time_ago(dt: datetime | None) -> str | None:
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = delta.total_seconds()
        if seconds < 0:
            return "刚刚"
        if seconds < 3600:
            return f"{int(seconds // 60)}分钟前"
        if seconds < 86400:
            return f"{int(seconds // 3600)}小时前"
        days = int(seconds // 86400)
        if days < 30:
            return f"{days}天前"
        if days < 365:
            return f"{days // 30}个月前"
        return f"{days // 365}年前"

    @staticmethod
    def _score_bar(score: float, segments: int = 10) -> str:
        filled = max(0, min(segments, round(score)))
        return "▰" * filled + "▱" * (segments - filled)

    @staticmethod
    def card(item: ContentItem, index: int) -> str:
        title = escape(item.title)
        title_link = f'<a href="{item.url}">{title}</a>'
        category = item.category or "ai"
        cat_icon = NewsProjectRenderer._category_icon(category)
        cat_label = service.get_category_label(category)
        source = item.source
        src_icon = NewsProjectRenderer._source_icon(source)

        meta_parts = [f"{src_icon} {escape(cat_label)}"]
        
        if source == "hacker_news":
            hn_score = item.metrics.get("hn_score", 0)
            hn_comments = item.metrics.get("hn_comments", 0)
            meta_parts.append(f"⬆️ <code>{NewsProjectRenderer._format_count(hn_score)}</code>")
            if hn_comments:
                meta_parts.append(f"💬 <code>{NewsProjectRenderer._format_count(hn_comments)}</code>")
        
        if item.author:
            meta_parts.append(f"🧑‍💻 {escape(item.author)}")
        
        meta_line = "     ".join(meta_parts)

        sub_meta_parts = []
        
        published_at = item.metrics.get("published_at")
        if published_at:
            try:
                dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                time_label = NewsProjectRenderer._time_ago(dt)
                if time_label:
                    sub_meta_parts.append(f"🕐 {time_label}")
            except Exception:
                pass
        
        if item.metrics.get("tags"):
            tags = item.metrics["tags"][:3]
            tag_str = " ".join(f"<code>#{escape(t.replace(' ', '_'))}</code>" for t in tags)
            sub_meta_parts.append(f"🏷 {tag_str}")

        sub_meta_line = f"\n{chr(10).join(sub_meta_parts)}" if sub_meta_parts else ""
        header = f"<b>{index}. {title_link}</b>\n{meta_line}{sub_meta_line}"

        detail_lines = []
        if item.summary:
            s = item.summary
            if s.score and s.score > 0:
                bar = NewsProjectRenderer._score_bar(s.score)
                score_line = f"{bar} <b>{s.score:.1f}</b>/10"
                maturity = getattr(s, "maturity_level", None)
                if maturity:
                    maturity_text = escape(str(maturity).strip())
                    if len(maturity_text) > 40:
                        maturity_text = maturity_text[:40] + "…"
                    score_line += f" · {maturity_text}"
                detail_lines.append(score_line)

            if s.one_liner:
                detail_lines.append(escape(s.one_liner))

            if s.use_cases:
                uc_text = escape(str(s.use_cases).strip())
                if len(uc_text) > 200:
                    uc_text = uc_text[:200] + "…"
                detail_lines.append(f"🎯 {uc_text}")

            key_points = s.get_pros_list() if hasattr(s, 'get_pros_list') else []
            if key_points:
                detail_lines.append(f"🔑 {escape(str(key_points[0]).strip())}")
        elif item.description:
            desc_text = escape(item.description.strip())
            if len(desc_text) > 200:
                desc_text = desc_text[:200] + "…"
            detail_lines.append(desc_text)

        if not detail_lines:
            return header

        body = "\n".join(detail_lines)
        return f"{header}\n<blockquote expandable>{body}</blockquote>"

    @staticmethod
    def page(items: list[ContentItem], all_count: int, page: int, total_pages: int, header: str) -> str:
        lines = [header, ""]
        start_index = page * PAGE_SIZE
        for i, item in enumerate(items, start=start_index + 1):
            lines.append(NewsProjectRenderer.card(item, i))
            lines.append("")
        lines.append(f"📊 共 <b>{all_count}</b> 条 · 第 <b>{page + 1}/{total_pages}</b> 页")
        return "\n".join(lines)

    @staticmethod
    def empty(message: str) -> str:
        return f"📊 {escape(message)}\n\n请稍后再试"


class NewsKeyboards:
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔥 近期热门", callback_data="news_recent_hot"),
                InlineKeyboardButton(text="📈 趋势", callback_data="news_trending_now"),
            ],
            [
                InlineKeyboardButton(text="🟠 Hacker News", callback_data="news_hacker_news"),
                InlineKeyboardButton(text="🔍 搜索", callback_data="news_search"),
            ],
            [
                InlineKeyboardButton(text="📁 按分类", callback_data="news_categories"),
                InlineKeyboardButton(text="❤️ 我的收藏", callback_data="news_my_favorites"),
            ],
            [InlineKeyboardButton(text="🔙 返回主菜单", callback_data="back_menu")],
        ])

    @staticmethod
    def categories_menu() -> InlineKeyboardMarkup:
        keyboard = []
        row = []
        for cat in service.get_categories():
            label = service.get_category_label(cat)
            row.append(InlineKeyboardButton(text=label, callback_data=f"news_cat_{cat}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton(text="🔙 返回菜单", callback_data="news_back_menu")])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def back_only() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回菜单", callback_data="news_back_menu")]
        ])

    @staticmethod
    def back_and_page(prefix: str, current: int, total: int) -> InlineKeyboardMarkup:
        kb = [
            [
                InlineKeyboardButton(text="⏪", callback_data=f"{prefix}:page:0"),
                InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:page:{current - 1}"),
                InlineKeyboardButton(text=f"{current + 1}/{total}", callback_data="do_nothing"),
                InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:page:{current + 1}"),
                InlineKeyboardButton(text="⏩", callback_data=f"{prefix}:page:{total - 1}"),
            ],
            [
                InlineKeyboardButton(text="🔄 刷新", callback_data=f"{prefix}:page:{current}"),
                InlineKeyboardButton(text="🔙 返回", callback_data="news_back_menu"),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=kb)

    @staticmethod
    def project_buttons(items: list[ContentItem], page: int) -> InlineKeyboardMarkup:
        start = page * PAGE_SIZE + 1
        rows = []
        for index, item in enumerate(items, start=start):
            short_id = _make_fav_id(item.id)
            title = item.title[:30] + "…" if len(item.title) > 30 else item.title
            rows.append([
                InlineKeyboardButton(text=f"❤️ {index}.{title}", callback_data=f"news_fav_add:{short_id}")
            ])
        return InlineKeyboardMarkup(inline_keyboard=rows)


class NewsPaginatedListView:
    def __init__(self, fetcher: Callable[[], Any], header: str, prefix: str, empty_message: str = "暂无新闻"):
        self.fetcher = fetcher
        self.header = header
        self.prefix = prefix
        self.empty_message = empty_message

    def _paginate(self, page: int):
        all_items = self.fetcher()
        total = len(all_items)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        items = all_items[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]
        return items, total, page, total_pages

    async def render(self, target, page: int = 0, edit: bool = True):
        try:
            items, total, page, total_pages = self._paginate(page)
            if not items:
                text = NewsProjectRenderer.empty(self.empty_message)
                markup = NewsKeyboards.back_only()
            else:
                text = NewsProjectRenderer.page(items, total, page, total_pages, self.header)
                markup = NewsKeyboards.back_and_page(self.prefix, page, total_pages)

            fav = NewsKeyboards.project_buttons(items, page)
            markup = InlineKeyboardMarkup(
                inline_keyboard=fav.inline_keyboard + markup.inline_keyboard
            )
            await self._send(target, text, markup, edit)

        except Exception as e:
            logger.error(f"[{self.prefix}] 加载失败: {e}")
            try:
                await self._send(target, "❌ 加载失败，请重新打开菜单再试", NewsKeyboards.back_only(), edit=False)
            except Exception as inner_e:
                logger.error(f"[{self.prefix}] 发送错误提示也失败: {inner_e}")

    @staticmethod
    async def _send(target, text: str, markup: InlineKeyboardMarkup, edit: bool):
        kwargs = dict(parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
        if not edit:
            await target.answer(text, **kwargs)
            return

        try:
            await target.edit_text(text, **kwargs)
        except TelegramBadRequest as e:
            error_msg = str(e)
            if "message is not modified" in error_msg:
                logger.debug("内容未变化，跳过编辑")
                return
            if "message can't be edited" in error_msg or "message to edit not found" in error_msg:
                logger.warning(f"消息无法编辑，改为发送新消息: {error_msg}")
                await target.answer(text, **kwargs)
                return
            raise


class NewsProjectViews:
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return NewsKeyboards.main_menu()

    @staticmethod
    def recent_hot() -> NewsPaginatedListView:
        return NewsPaginatedListView(
            fetcher=lambda: service.get_recent_hot(days=7),
            header="🔥 <b>近期热门新闻</b>",
            prefix="news_recent_hot",
        )

    @staticmethod
    def trend() -> NewsPaginatedListView:
        return NewsPaginatedListView(
            fetcher=lambda: service.get_trend(days=3),
            header="📈 <b>趋势新闻</b>",
            prefix="news_trending_now",
        )

    @staticmethod
    def hacker_news() -> NewsPaginatedListView:
        return NewsPaginatedListView(
            fetcher=lambda: service.get_hacker_news(limit=200),
            header="🟠 <b>Hacker News 热门</b>",
            prefix="news_hacker_news",
        )

    @staticmethod
    def by_category(category: str) -> NewsPaginatedListView:
        label = service.get_category_label(category)
        return NewsPaginatedListView(
            fetcher=lambda: service.get_by_category(category, limit=200),
            header=f"<b>{escape(label)}</b>",
            prefix=f"news_cat_{category}",
            empty_message=f"{label} 暂无新闻",
        )

    @staticmethod
    def my_favorites(telegram_id: int) -> NewsPaginatedListView:
        return NewsPaginatedListView(
            fetcher=lambda: service.get_user_favorites(telegram_id),
            header="❤️ <b>我的收藏</b>",
            prefix="news_my_favorites",
            empty_message="你还没有收藏任何新闻",
        )

    @staticmethod
    def search(keyword: str, search_id: str) -> NewsPaginatedListView:
        return NewsPaginatedListView(
            fetcher=lambda: service.search(keyword, limit=200),
            header=f"🔍 <b>'{escape(keyword)}' 搜索结果</b>",
            prefix=f"news_search_{search_id}",
            empty_message=f"未找到关于 '{keyword}' 的新闻",
        )

    @staticmethod
    async def get_page_views(prefix: str, query, page):
        if prefix == "news_recent_hot":
            await NewsProjectViews.recent_hot().render(query.message, page=page)
        elif prefix == "news_trending_now":
            await NewsProjectViews.trend().render(query.message, page=page)
        elif prefix == "news_hacker_news":
            await NewsProjectViews.hacker_news().render(query.message, page=page)
        elif prefix == "news_my_favorites":
            await NewsProjectViews.my_favorites(query.from_user.id).render(query.message, page=page)
        elif prefix.startswith("news_cat_"):
            category = prefix.split("news_cat_", 1)[1]
            await NewsProjectViews.by_category(category).render(query.message, page=page)
        elif prefix.startswith("news_search_"):
            search_id = prefix.split("news_search_", 1)[1]
            keyword = _search_cache.get(search_id)
            if keyword:
                await NewsProjectViews.search(keyword, search_id).render(query.message, page=page)


_search_cache: dict[str, str] = {}


def _make_search_id(telegram_id: int) -> str:
    return str(telegram_id)


def _extract_page(callback_data: str) -> int:
    parts = callback_data.split(":")
    if len(parts) == 3 and parts[1] == "page":
        try:
            return int(parts[2])
        except ValueError:
            return 0
    return 0


@router.message(Command("news_start"))
async def cmd_start(message: Message):
    welcome_text = (
        "<b>📰 AI/科技新闻聚合</b>\n"
        "每日精选 Hacker News、TechCrunch、VentureBeat、arXiv 等优质源\n"
        "涵盖 AI、大模型、研究论文、工具框架、行业动态\n"
        "选择下方分类开始浏览 ↓"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=NewsKeyboards.main_menu())


@router.message(Command("news_recent_hot"))
@router.callback_query(F.data == "news_recent_hot")
async def recent_hot(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await NewsProjectViews.recent_hot().render(target, page=0)


@router.message(Command("news_trending_now"))
@router.callback_query(F.data == "news_trending_now")
async def trending_now(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await NewsProjectViews.trend().render(target, page=0)


@router.message(Command("news_hacker_news"))
@router.callback_query(F.data == "news_hacker_news")
async def hacker_news(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await NewsProjectViews.hacker_news().render(target, page=0)


@router.callback_query(F.data == "news_categories")
async def categories_menu(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "📁 <b>按分类浏览</b>\n\n选择你感兴趣的新闻分类：",
        parse_mode="HTML",
        reply_markup=NewsKeyboards.categories_menu(),
    )


@router.callback_query(F.data.startswith("news_cat_"))
async def by_category(query: CallbackQuery):
    if not query.data:
        return
    await query.answer("🔄 加载中...")
    base = query.data.split(":")[0]
    category = base.split("news_cat_", 1)[1]
    page = _extract_page(query.data)
    await NewsProjectViews.by_category(category).render(query.message, page=page)


@router.message(Command("news_my_favorites"))
@router.callback_query(F.data == "news_my_favorites")
async def my_favorites(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await NewsProjectViews.my_favorites(event.from_user.id).render(target, page=0)


@router.message(Command("news_search"))
@router.callback_query(F.data == "news_search")
async def search_start(event: Union[Message, CallbackQuery]):
    prompt = "🔍 请输入搜索关键词：\n例如：GPT、RAG、fine-tuning、transformer 等"
    if isinstance(event, CallbackQuery):
        await event.answer()
        set_pending(event.from_user.id, "news")
        await event.message.edit_text(text=prompt, reply_markup=NewsKeyboards.back_only())
    else:
        set_pending(event.from_user.id, "news")
        await event.answer(text=prompt, reply_markup=NewsKeyboards.back_only())


@router.message(F.text, is_pending("news"))
async def search_news_text(message: Message):
    keyword = message.text.strip()
    clear_pending(message.from_user.id)
    if len(keyword) < 2:
        await message.reply("⚠️ 搜索词至少 2 个字符")
        return

    search_id = _make_search_id(message.from_user.id)
    _search_cache[search_id] = keyword
    view = NewsProjectViews.search(keyword, search_id)
    await view.render(message, page=0, edit=False)


@router.callback_query(F.data.startswith("news_search_"))
async def search_paginate(query: CallbackQuery):
    if not query.data:
        return
    await query.answer("🔄 加载中...")
    base = query.data.split(":")[0]
    search_id = base.split("news_search_", 1)[1]
    keyword = _search_cache.get(search_id)

    if not keyword:
        await query.message.edit_text(
            text="⚠️ 搜索会话已过期，请重新输入关键词搜索",
            reply_markup=NewsKeyboards.back_only(),
        )
        return
    page = _extract_page(query.data)
    await NewsProjectViews.search(keyword, search_id).render(query.message, page=page)


@router.callback_query(F.data.startswith("news_fav_add:"))
async def add_favorite(query: CallbackQuery):
    short_id = query.data.split(":", 1)[1]
    item_id_str = _get_fav_id(short_id)
    if not item_id_str:
        await query.answer("⚠️ 收藏信息已过期，请刷新列表后重试")
        return
    item_id = int(item_id_str)
    result = service.toggle_favorite(telegram_id=query.from_user.id, item_id=item_id)
    if result.get("action") == "removed":
        await query.answer("❤️ 已取消收藏")
    else:
        await query.answer("❤️ 已收藏")


@router.callback_query(F.data == "news_back_menu")
async def back_menu(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "👋 欢迎使用 <b>AI/科技新闻聚合</b>！\n\n选择你感兴趣的内容：",
        parse_mode="HTML",
        reply_markup=NewsKeyboards.main_menu(),
    )


@router.message(Command("news_help"))
async def cmd_help(message: Message):
    help_text = (
        "<b>📖 AI/科技新闻聚合 Bot 使用帮助</b>\n\n"
        "<b>🔍 主要功能：</b>\n"
        "• <code>/news_start</code> - 打开主菜单\n"
        "• <code>/news_recent_hot</code> - 查看近期热门新闻\n"
        "• <code>/news_trending_now</code> - 查看趋势新闻\n"
        "• <code>/news_hacker_news</code> - 查看 Hacker News 热门\n"
        "• <code>/news_search</code> - 搜索新闻（支持关键词）\n"
        "• <code>/news_my_favorites</code> - 查看你收藏的新闻\n\n"
        "<b>📁 分类浏览：</b>\n"
        "• 🤖 AI/机器学习 - 通用 AI 新闻\n"
        "• 🧠 大语言模型 - LLM 相关\n"
        "• 📄 研究论文 - arXiv 等学术内容\n"
        "• 🔧 工具/框架 - 开发工具、库发布\n"
        "• 🏢 行业动态 - 融资、收购、商业新闻\n\n"
        "<b>🎯 使用技巧：</b>\n"
        "• 点击主菜单中的按钮即可快速浏览\n"
        "• 支持分页查看（⬅️ 上一页 / 下一页 ➡️）\n"
        "• 新闻详情中可点击收藏❤️\n"
        "• 数据来源：Hacker News、TechCrunch、VentureBeat、The Verge、arXiv\n\n"
        "有任何问题或建议，欢迎随时反馈！"
    )
    await message.answer(
        help_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=NewsKeyboards.main_menu(),
    )