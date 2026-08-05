import hashlib
from datetime import datetime, timezone
from html import escape
from typing import Any, Callable, Union

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

from app.api.huggingface_client import HuggingFaceClient
from app.database.models import ContentItem
from app.logger import AppLogger
from app.bot.search_state import set_pending, clear_pending, is_pending
from app.services.huggingface_service import HuggingFaceService

logger = AppLogger.get(__name__)
router = Router()
PAGE_SIZE = 6

service = HuggingFaceService()


class HuggingfaceProjectRenderer:
    PIPELINE_ICONS = {
        "text-generation": "📝",
        "text-to-image": "🖼",
        "text-to-video": "🎬",
        "text-to-speech": "🔊",
        "image-text-to-text": "🧩",
    }

    @staticmethod
    def _pipeline_icon(pipeline_tag: str | None) -> str:
        if not pipeline_tag:
            return "🤖"
        return HuggingfaceProjectRenderer.PIPELINE_ICONS.get(pipeline_tag, "🤖")

    @staticmethod
    def _format_count(n: int | None) -> str:
        n = n or 0
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    @staticmethod
    def _time_ago(value: Any) -> str | None:
        if not value:
            return None
        dt = value
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(dt, datetime):
            return None
        now = datetime.now(timezone.utc)
        seconds = (now - dt).total_seconds()
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
    def _get_license_info(license_name: str | None) -> tuple[str, str]:
        """
        返回 (icon, label)
        使用分级匹配策略
        """
        if not license_name:
            return "📄", "未指定"

        name = license_name.lower()

        # 1. 精确匹配
        exact_map = {
            "mit": "🔓",
            "apache-2.0": "📜",
            "apache license 2.0": "📜",
            "gpl-3.0": "©️",
            "gnu general public license v3.0": "©️",
            "gpl-2.0": "©️",
            "gnu general public license v2.0": "©️",
            "bsd-3-clause": "🅱️",
            "bsd 3-clause \"new\" or \"revised\" license": "🅱️",
            "mpl-2.0": "🦊",
            "mozilla public license 2.0": "🦊",
        }
        if name in exact_map:
            return exact_map[name], license_name

        # 2. 关键词模糊匹配
        keyword_map = {
            "mit": "🔓",
            "apache": "📜",
            "gpl": "©️",
            "bsd": "🅱️",
            "mozilla": "🦊",
            "creative commons": "🎨",
            "cc-": "🎨",
        }
        for kw, icon in keyword_map.items():
            if kw in name:
                return icon, license_name

        return "📄", license_name

    @staticmethod
    def card(model: ContentItem, index: int) -> str:
        model_id = model.external_id
        name = model_id.split("/")[-1]
        author = model_id.split("/")[0] if "/" in model_id else None
        url = f"https://huggingface.co/{model_id}"
        title_link = f'<a href="{escape(url)}">{escape(name)}</a>'

        pipeline_tag = model.metrics["pipeline_tag"]
        icon = HuggingfaceProjectRenderer._pipeline_icon(pipeline_tag)

        meta_parts = [f"👍 <code>{HuggingfaceProjectRenderer._format_count(model.metrics['likes'])}</code>"]
        downloads = model.metrics["downloads"]
        if downloads:
            meta_parts.append(f"⬇️ <code>{HuggingfaceProjectRenderer._format_count(downloads)}</code>")
        meta_parts.append(f"{icon} {escape(pipeline_tag) if pipeline_tag else '未分类'}")
        if author:
            meta_parts.append(f"🧑‍💻 {escape(author)}")
        meta_line = "     ".join(meta_parts)

        sub_meta_parts = []
        library = model.metrics["library_name"]
        if library:
            sub_meta_parts.append(f"📦 {escape(str(library))}")

        # 使用新的解析方法
        license_name = model.metrics.get("license")
        license_icon, license_label = HuggingfaceProjectRenderer._get_license_info(license_name)
        sub_meta_parts.append(f"{license_icon} {escape(license_label)}")

        if model.metrics["private"]:
            sub_meta_parts.append("🔐 私有模型")

        sub_meta_line = f"\n{chr(10).join(sub_meta_parts)}" if sub_meta_parts else ""
        header = f"<b>{index}. {title_link}</b>\n{meta_line}{sub_meta_line}"

        detail_lines = []
        tags = [t for t in (model.metrics["tags"] or []) if isinstance(t, str)][:6]
        if tags:
            tag_str = " ".join(f"<code>#{escape(t.replace(' ', '_'))}</code>" for t in tags)
            detail_lines.append(f"🏷 {tag_str}")

        if not detail_lines:
            return header
        body = "\n".join(detail_lines)
        return f"{header}\n<blockquote expandable>{body}</blockquote>"

    @staticmethod
    def page(models: list[ContentItem], all_count: int, page: int, total_pages: int, header: str) -> str:
        lines = [header, ""]
        start_index = page * PAGE_SIZE
        for i, model in enumerate(models, start=start_index + 1):
            lines.append(HuggingfaceProjectRenderer.card(model, i))
            lines.append("")
        lines.append(f"📊 共 <b>{all_count}</b> 个模型 · 第 <b>{page + 1}/{total_pages}</b> 页")
        return "\n".join(lines)

    @staticmethod
    def empty(message: str) -> str:
        return f"📊 {escape(message)}\n\n请稍后再试"


_fav_id_cache: dict[str, str] = {}


def _make_fav_id(model_id: str) -> str:
    short_id = hashlib.md5(model_id.encode()).hexdigest()[:10]
    _fav_id_cache[short_id] = model_id
    return short_id


class HuggingfaceKeyboards:
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔥 近期热门", callback_data="huggingface_recent_hot"),
                InlineKeyboardButton(text="🏆 长期热门", callback_data="huggingface_longtime_hot"),
            ],
            [
                InlineKeyboardButton(text="📈 Trending 趋势", callback_data="huggingface_trending_now"),
                InlineKeyboardButton(text="🔍 搜索模型", callback_data="huggingface_search"),
            ],
            [
                InlineKeyboardButton(text="❤️ 我的收藏", callback_data="huggingface_my_favorites"),
                InlineKeyboardButton(text="📁 更多分类", callback_data="huggingface_more_categories"),
            ],
            [InlineKeyboardButton(text="🔙 返回主菜单", callback_data="back_menu")],
        ])

    @staticmethod
    def get_more_categories_keyboard() -> InlineKeyboardMarkup:
        keyboard = []
        row = []
        for cn_name, pipeline_tag in HuggingFaceClient.PIPELINE_MAP.items():
            row.append(InlineKeyboardButton(text=cn_name, callback_data=f"huggingface_pipeline_{pipeline_tag}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton(text="🔙 返回菜单", callback_data="huggingface_back_menu")])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def back_only() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回菜单", callback_data="huggingface_back_menu")]
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
                InlineKeyboardButton(text="🔙 返回", callback_data="huggingface_back_menu"),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=kb)

    @staticmethod
    def project_buttons(models: list[ContentItem], page: int) -> InlineKeyboardMarkup:
        start = page * PAGE_SIZE + 1
        rows = []
        for index, model in enumerate(models, start=start):
            model_id = model.external_id
            if not model_id:
                continue
            short_id = _make_fav_id(model_id)
            name = model_id.split("/")[-1]
            rows.append([
                InlineKeyboardButton(text=f"❤️ {index}.{name}", callback_data=f"huggingface_fav_add:{short_id}")
            ])
        return InlineKeyboardMarkup(inline_keyboard=rows)


class HuggingfacePaginatedListView:
    """
    通用分页列表视图（async 版）。
    fetcher: Callable[[], Awaitable[list[dict]]] —— 返回全部模型（用于计算总页数）
    """

    def __init__(self, fetcher: Callable[[], Any], header: str, prefix: str, empty_message: str = "暂无模型"):
        self.fetcher = fetcher
        self.header = header
        self.prefix = prefix
        self.empty_message = empty_message

    def _paginate(self, page: int):
        all_models = self.fetcher()
        total = len(all_models)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        models = all_models[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]
        return models, total, page, total_pages

    async def render(self, target, page: int = 0, edit: bool = True):
        try:
            models, total, page, total_pages = self._paginate(page)
            if not models:
                text = HuggingfaceProjectRenderer.empty(self.empty_message)
                markup = HuggingfaceKeyboards.back_only()
            else:
                text = HuggingfaceProjectRenderer.page(models, total, page, total_pages, self.header)
                markup = HuggingfaceKeyboards.back_and_page(self.prefix, page, total_pages)

            fav = HuggingfaceKeyboards.project_buttons(models, page)
            markup = InlineKeyboardMarkup(inline_keyboard=fav.inline_keyboard + markup.inline_keyboard)
            await self._send(target, text, markup, edit)

        except Exception as e:
            logger.error(f"[{self.prefix}] 加载失败: {e}")
            try:
                await self._send(target, "❌ 加载失败，请重新打开菜单再试", HuggingfaceKeyboards.back_only(), edit=False)
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


class HuggingfaceProjectViews:
    """集中定义各类列表视图的来源，便于维护和扩展"""

    @staticmethod
    def recent_hot() -> HuggingfacePaginatedListView:
        return HuggingfacePaginatedListView(
            fetcher=lambda: service.get_latest_ranking(limit=100),
            header="🔥 <b>近期热门模型</b>",
            prefix="huggingface_recent_hot",
        )

    @staticmethod
    def longtime_hot() -> HuggingfacePaginatedListView:
        return HuggingfacePaginatedListView(
            fetcher=lambda: service.get_total_hot(limit=200),
            header="🏆 <b>长期热门模型</b>",
            prefix="huggingface_longtime_hot",
        )

    @staticmethod
    def trend() -> HuggingfacePaginatedListView:
        return HuggingfacePaginatedListView(
            fetcher=lambda: service.get_trend(limit=100),
            header="📈 <b>Trending 趋势</b>",
            prefix="huggingface_trending_now",
        )

    @staticmethod
    def by_pipeline(pipeline_tag: str) -> HuggingfacePaginatedListView:
        cn_name = next(
            (k for k, v in HuggingFaceClient.PIPELINE_MAP.items() if v == pipeline_tag),
            pipeline_tag,
        )
        return HuggingfacePaginatedListView(
            fetcher=lambda: service.get_latest_ranking(pipeline_tag, limit=200),
            header=f"<b>{escape(cn_name)}</b> 模型",
            prefix=f"huggingface_pipeline_{pipeline_tag}",
            empty_message=f"{cn_name} 暂无模型",
        )

    @staticmethod
    def my_favorites(telegram_id: int) -> HuggingfacePaginatedListView:
        return HuggingfacePaginatedListView(
            fetcher=lambda: service.get_user_favorites(telegram_id),
            header="❤️ <b>我的收藏</b>",
            prefix="huggingface_my_favorites",
            empty_message="你还没有收藏任何模型，浏览时可以点击收藏按钮添加",
        )

    @staticmethod
    def search(keyword: str, search_id: str) -> HuggingfacePaginatedListView:
        return HuggingfacePaginatedListView(
            fetcher=lambda: service.search_models(keyword, limit=200),
            header=f"🔍 <b>'{escape(keyword)}' 搜索结果</b>",
            prefix=f"huggingface_search_{search_id}",
            empty_message=f"未找到关于 '{keyword}' 的模型",
        )

    @staticmethod
    async def get_page_views(prefix: str, query, page):
        if prefix == "huggingface_recent_hot":
            await HuggingfaceProjectViews.recent_hot().render(query.message, page=page)
        elif prefix == "huggingface_longtime_hot":
            await HuggingfaceProjectViews.longtime_hot().render(query.message, page=page)
        elif prefix == "huggingface_trending_now":
            await HuggingfaceProjectViews.trend().render(query.message, page=page)
        elif prefix == "huggingface_my_favorites":
            await HuggingfaceProjectViews.my_favorites(query.from_user.id).render(query.message, page=page)
        elif prefix.startswith("huggingface_pipeline_"):
            pipeline_tag = prefix.split("huggingface_pipeline_", 1)[1]
            await HuggingfaceProjectViews.by_pipeline(pipeline_tag).render(query.message, page=page)
        elif prefix.startswith("huggingface_search_"):
            search_id = prefix.split("huggingface_search_", 1)[1]
            keyword = _search_cache.get(search_id)
            if keyword:
                await HuggingfaceProjectViews.search(keyword, search_id).render(query.message, page=page)


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


@router.message(Command("huggingface_start"))
async def cmd_start(message: Message):
    welcome_text = (
        "<b>🤗 HuggingFace 模型探索</b>\n"
        "发现最有潜力的开源 AI 模型\n"
        "涵盖文字生成、图片生成、语音合成等多种类型\n"
        "选择下方分类开始探索吧 ↓"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=HuggingfaceKeyboards.main_menu())


@router.message(Command("huggingface_recent_hot"))
@router.callback_query(F.data == "huggingface_recent_hot")
async def recent_hot(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await HuggingfaceProjectViews.recent_hot().render(target, page=0)


@router.message(Command("huggingface_longtime_hot"))
@router.callback_query(F.data == "huggingface_longtime_hot")
async def longtime_hot(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await HuggingfaceProjectViews.longtime_hot().render(target, page=0)


@router.message(Command("huggingface_trending_now"))
@router.callback_query(F.data == "huggingface_trending_now")
async def trending_now(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await HuggingfaceProjectViews.trend().render(target, page=0)


@router.message(Command("huggingface_my_favorites"))
@router.callback_query(F.data == "huggingface_my_favorites")
async def my_favorites(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await HuggingfaceProjectViews.my_favorites(event.from_user.id).render(target, page=0)


@router.callback_query(F.data.startswith("huggingface_pipeline_"))
async def by_pipeline(query: CallbackQuery):
    if not query.data:
        return
    await query.answer("🔄 加载中...")
    base = query.data.split(":")[0]
    pipeline_tag = base.split("huggingface_pipeline_", 1)[1]
    page = _extract_page(query.data)
    await HuggingfaceProjectViews.by_pipeline(pipeline_tag).render(query.message, page=page)


@router.message(Command("huggingface_search"))
@router.callback_query(F.data == "huggingface_search")
async def search_start(event: Union[Message, CallbackQuery]):
    prompt = "🔍 请输入要搜索的模型关键词：\n例如：llama、stable-diffusion、whisper 等"
    if isinstance(event, CallbackQuery):
        await event.answer()
        set_pending(event.from_user.id, "huggingface")
        await event.message.edit_text(text=prompt, reply_markup=HuggingfaceKeyboards.back_only())
    else:
        set_pending(event.from_user.id, "huggingface")
        await event.answer(text=prompt, reply_markup=HuggingfaceKeyboards.back_only())


@router.message(F.text, is_pending("huggingface"))
async def search_models_text(message: Message):
    keyword = message.text.strip()
    clear_pending(message.from_user.id)
    if len(keyword) < 2:
        await message.reply("⚠️ 搜索词至少 2 个字符")
        return

    search_id = _make_search_id(message.from_user.id)
    _search_cache[search_id] = keyword
    view = HuggingfaceProjectViews.search(keyword, search_id)
    await view.render(message, page=0, edit=False)


@router.callback_query(F.data.startswith("huggingface_search_"))
async def search_paginate(query: CallbackQuery):
    if not query.data:
        return
    await query.answer("🔄 加载中...")
    base = query.data.split(":")[0]
    search_id = base.split("huggingface_search_", 1)[1]
    keyword = _search_cache.get(search_id)

    if not keyword:
        await query.message.edit_text(
            text="⚠️ 搜索会话已过期，请重新输入关键词搜索",
            reply_markup=HuggingfaceKeyboards.back_only(),
        )
        return
    page = _extract_page(query.data)
    await HuggingfaceProjectViews.search(keyword, search_id).render(query.message, page=page)


@router.callback_query(F.data.startswith("huggingface_fav_add:"))
async def add_favorite(query: CallbackQuery):
    short_id = query.data.split(":", 1)[1]
    model_id = _fav_id_cache.get(short_id)
    if not model_id:
        await query.answer("⚠️ 收藏信息已过期，请刷新列表后重试")
        return
    result = service.toggle_favorite(telegram_id=query.from_user.id, model_id=model_id)
    if result.get("action") == "removed":
        await query.answer("❤️ 已取消收藏")
    else:
        await query.answer("❤️ 已收藏")


@router.callback_query(F.data == "huggingface_back_menu")
async def back_menu(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "👋 欢迎使用 <b>HuggingFace 模型推荐 Bot</b>！\n\n选择你感兴趣的内容：",
        parse_mode="HTML",
        reply_markup=HuggingfaceKeyboards.main_menu(),
    )


@router.callback_query(F.data == "huggingface_more_categories")
async def more_categories(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "📁 <b>更多分类</b>\n\n选择你感兴趣的模型类型：",
        parse_mode="HTML",
        reply_markup=HuggingfaceKeyboards.get_more_categories_keyboard(),
    )


@router.message(Command("huggingface_help"))
async def cmd_help(message: Message):
    help_text = (
        "<b>📖 HuggingFace 模型推荐 Bot 使用帮助</b>\n\n"
        "<b>🔍 主要功能：</b>\n"
        "• <code>/huggingface_start</code> - 打开主菜单\n"
        "• <code>/huggingface_recent_hot</code> - 查看近期热门模型\n"
        "• <code>/huggingface_longtime_hot</code> - 查看长期热门模型（按点赞数）\n"
        "• <code>/huggingface_trending_now</code> - 查看当前 Trending 模型\n"
        "• <code>/huggingface_search</code> - 搜索模型（支持关键词）\n"
        "• <code>/huggingface_my_favorites</code> - 查看你收藏的模型\n\n"
        "<b>🎯 使用技巧：</b>\n"
        "• 点击主菜单中的按钮即可快速浏览\n"
        "• 支持分页查看（⬅️ 上一页 / 下一页 ➡️）\n"
        "• 模型详情中可点击收藏❤️\n"
        "• «更多分类» 里可以按 文字生成/文字转图片/文字转影片/语音合成/多模态 筛选\n\n"
        "有任何问题或建议，欢迎随时反馈！"
    )
    await message.answer(
        help_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=HuggingfaceKeyboards.main_menu(),
    )