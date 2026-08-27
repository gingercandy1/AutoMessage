from datetime import timezone, datetime
from html import escape
from typing import Any, Callable, Union

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

from app.bot.search_state import clear_pending, is_pending, set_pending
from app.bot.renderers.base import BaseProjectRenderer
from app.logger import AppLogger
from app.services.github_service import GithubService

logger = AppLogger.get(__name__)
router = Router()
service = GithubService()
PAGE_SIZE = 6


class GitHubProjectRenderer(BaseProjectRenderer):
    LANGUAGE_ICONS = {
        "python": "🐍", "java": "☕", "rust": "🦀", "go": "🐹",
        "javascript": "🟨", "typescript": "🔷", "c#": "💎", "php": "🐘",
        "swift": "🍎", "kotlin": "🟣", "c++": "🔵", "c": "⚙️",
        "ruby": "💎", "html": "🌐", "css": "🎨", "shell": "🐚",
    }

    @staticmethod
    def _language_icon(language: str | None) -> str:
        if not language:
            return "📦"
        return GitHubProjectRenderer.LANGUAGE_ICONS.get(language.lower(), "📦")

    @staticmethod
    def _score_bar(score: float, segments: int = 10) -> str:
        filled = max(0, min(segments, round(score)))
        return "▰" * filled + "▱" * (segments - filled)

    @staticmethod
    def card(project, index: int) -> str:
        """
        生成列表页卡片文字，紧凑展示，适合一条消息里放多个项目
        """
        title = escape(project.title)
        title_link = f'<a href="{escape(project.url)}">{title}</a>'
        # JSONB 老数据可能缺键，统一用 .get() 防 KeyError 导致整个列表渲染失败
        language = project.metrics.get("language")
        lang_label = escape(language) if language else "未分类"
        lang_icon = GitHubProjectRenderer._language_icon(language)

        meta_parts = [f"⭐ <code>{GitHubProjectRenderer._format_count(project.metrics.get("stars"))}</code>"]
        if project.metrics.get("forks"):
            meta_parts.append(f"🍴 <code>{GitHubProjectRenderer._format_count(project.metrics.get("forks"))}</code>")
        meta_parts.append(f"{lang_icon} {lang_label}")
        if project.author:
            meta_parts.append(f"🧑‍💻 {escape(project.author)}")
        meta_line = "     ".join(meta_parts)

        # 第二行元信息：license + 更新时间 + 近期活跃度
        sub_meta_parts = []

        license = project.metrics.get("license")
        if license:
            license_icon = GitHubProjectRenderer._license_icon(license)
            license_label = escape(license)
            sub_meta_parts.append(f"{license_icon} {license_label}")

        updated_at = project.metrics.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        updated_label = GitHubProjectRenderer._time_ago(updated_at)
        if updated_label:
            sub_meta_parts.append(f"🕐 {updated_label}更新")

        commits_last_4_weeks = project.metrics.get("commits_last_4_weeks")
        if commits_last_4_weeks:
            sub_meta_parts.append(f"📈 近4周 {commits_last_4_weeks} 次提交")

        sub_meta_line = f"\n{'\n'.join(sub_meta_parts)}" if sub_meta_parts else ""
        header = f"<b>{index}. {title_link}</b>\n{meta_line}{sub_meta_line}"

        detail_lines = []
        if project.summary:
            s = project.summary

            if s.score and s.score > 0:
                bar = GitHubProjectRenderer._score_bar(s.score)
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

            pros = GitHubProjectRenderer._safe_get_list(s, "pros")
            cons = GitHubProjectRenderer._safe_get_list(s, "cons")
            if pros:
                detail_lines.append(f"🟢 {escape(str(pros[0]).strip())}")
            if cons:
                detail_lines.append(f"🟠 {escape(str(cons[0]).strip())}")

            tags = GitHubProjectRenderer._safe_get_list(s, "tags")
            if tags:
                tag_str = " ".join(
                    f"<code>#{escape(str(t).strip().replace(' ', '_'))}</code>"
                    for t in tags[:5] if t
                )
                if tag_str:
                    detail_lines.append(f"🏷 {tag_str}")
        elif project.description:
            desc_text = escape(project.description.strip())
            if len(desc_text) > 200:
                desc_text = desc_text[:200] + "…"
            detail_lines.append(desc_text)

        if not detail_lines:
            return header

        body = "\n".join(detail_lines)
        return f"{header}\n<blockquote expandable>{body}</blockquote>"

    @staticmethod
    def page(projects, all_count: int, page: int, total_pages: int, header: str) -> str:
        lines = [header, ""]
        start_index = page * PAGE_SIZE
        for i, project in enumerate(projects, start=start_index + 1):
            lines.append(GitHubProjectRenderer.card(project, i))
            lines.append("")
        lines.append(f"📊 共 <b>{all_count}</b> 个项目 · 第 <b>{page + 1}/{total_pages}</b> 页")
        return "\n".join(lines)

    @staticmethod
    def empty(message: str) -> str:
        return f"📊 {escape(message)}\n\n请稍后再试"

class GitHubKeyboards:
    """所有键盘统一管理"""

    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🌐 AI / 大模型", callback_data="github_tag_ai"),
            ],
            [
                InlineKeyboardButton(text="🔥 近期热门", callback_data="github_recent_hot"),
                InlineKeyboardButton(text="🏆 长期热门", callback_data="github_longtime_hot")
            ],
            [
                InlineKeyboardButton(text="📈 Trending 趋势", callback_data="github_trending_now"),
                InlineKeyboardButton(text="🔍 搜索项目", callback_data="github_search")
            ],
            [
                InlineKeyboardButton(text="❤️ 我的收藏", callback_data="github_my_favorites"),
                InlineKeyboardButton(text="📁 更多分类", callback_data="github_more_categories")
            ],
        ])


    @staticmethod
    def get_more_categories_keyboard():
        keyboard = [
            [
                InlineKeyboardButton(text="💻 C++", callback_data="github_lang_c++"),
                InlineKeyboardButton(text="⚙️ C", callback_data="github_lang_c"),
            ],
            [
                InlineKeyboardButton(text="🐍 Python", callback_data="github_lang_python"),
                InlineKeyboardButton(text="☕ Java", callback_data="github_lang_java"),
            ],
            [
                InlineKeyboardButton(text="🦀 Rust", callback_data="github_lang_rust"),
                InlineKeyboardButton(text="📱 Go", callback_data="github_lang_go"),
            ],
            [
                InlineKeyboardButton(text="📦 JavaScript", callback_data="github_lang_javascript"),
                InlineKeyboardButton(text="🔷 TypeScript", callback_data="github_lang_typescript"),
            ],
            [
                InlineKeyboardButton(text="💎 C#", callback_data="github_lang_c#"),
                InlineKeyboardButton(text="📘 PHP", callback_data="github_lang_php"),
            ],
            [
                InlineKeyboardButton(text="🍎 Swift", callback_data="github_lang_swift"),
                InlineKeyboardButton(text="🟣 Kotlin", callback_data="github_lang_kotlin"),
            ],
            [
                InlineKeyboardButton(text="🌐 Web / 前端", callback_data="github_tag_web"),
                InlineKeyboardButton(text="🗄️ 数据库", callback_data="github_tag_database"),
            ],
            [
                InlineKeyboardButton(text="🔙 返回菜单", callback_data="back_menu"),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def back_only() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回菜单", callback_data="github_back_menu")]
        ])

    @staticmethod
    def back_and_page(prefix: str, current: int, total: int):
        kb = []
        nav = [InlineKeyboardButton(text="⏪", callback_data=f"{prefix}:page:0"),
               InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:page:{current - 1}"),
               InlineKeyboardButton(text=f"{current + 1}/{total}", callback_data="do_nothing"),
               InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:page:{current + 1}"),
               InlineKeyboardButton(text="⏩", callback_data=f"{prefix}:page:{total - 1}")]
        kb.append(nav)

        size_row = [
            InlineKeyboardButton(text="10", callback_data=f"{prefix}:page:{current + 10}"),
            InlineKeyboardButton(text="20", callback_data=f"{prefix}:page:{current + 20}"),
            InlineKeyboardButton(text="30", callback_data=f"{prefix}:page:{current + 30}"),
        ]
        kb.append(size_row)
        kb.append([
            InlineKeyboardButton(text="🔄 刷新", callback_data=f"{prefix}:page:{current}"),
            InlineKeyboardButton(text="🔙 返回", callback_data="back_menu")
        ])
        return InlineKeyboardMarkup(inline_keyboard=kb)

    @staticmethod
    def project_buttons(projects, page):
        start = page * PAGE_SIZE + 1
        fav = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"❤️ {index}.{project.title}",
                        callback_data=f"github_fav_add:{project.id}"
                    )
                ]
            for index, project in enumerate(projects, start=start)
            ]
        )
        return fav

class GitHubPaginatedListView:
    """
    通用分页列表视图。
    fetcher: Callable[[], list]  —— 返回该列表的全部项目（用于计算总页数）
    header: str                  —— 页面标题（HTML）
    prefix: str                  —— callback_data 前缀，用于翻页路由
    empty_message: str           —— 无数据时的提示文案
    """

    def __init__(self, fetcher: Callable[[], Any], header: str, prefix: str, empty_message: str = "暂无项目"):
        self.fetcher = fetcher
        self.header = header
        self.prefix = prefix
        self.empty_message = empty_message

    def _paginate(self, page: int):
        all_projects = self.fetcher()
        total = len(all_projects)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        projects = all_projects[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]
        return projects, total, page, total_pages

    async def render(self, target, page: int = 0, edit: bool = True):
        """
        target: CallbackQuery.message 或 Message，需要有 edit_text / answer
        edit: True 用 edit_text（回调场景），False 用 answer（普通消息回复场景）
        """
        try:
            projects, total, page, total_pages = self._paginate(page)
            if not projects:
                text = GitHubProjectRenderer.empty(self.empty_message)
                markup = GitHubKeyboards.back_only()
            else:
                text = GitHubProjectRenderer.page(projects, total, page, total_pages, self.header)
                markup = GitHubKeyboards.back_and_page(self.prefix, page, total_pages)

            fav = GitHubKeyboards.project_buttons(projects ,page)
            markup = InlineKeyboardMarkup(
                inline_keyboard=fav.inline_keyboard + markup.inline_keyboard
            )
            await self._send(target, text, markup, edit)

        except Exception as e:
            logger.error(f"[{self.prefix}] 加载失败: {e}")
            try:
                await self._send(target, f"❌ 加载失败，请重新打开菜单再试", GitHubKeyboards.back_only(), edit=False)
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

class GitHubProjectViews:
    """集中定义各类列表视图的来源，便于维护和扩展"""

    @staticmethod
    def recent_hot() -> GitHubPaginatedListView:
        return GitHubPaginatedListView(
            fetcher=lambda: service.get_recent_hot(day=14),
            header="🔥 <b>今日热门项目</b>",
            prefix="github_recent_hot",
            empty_message="暂无项目"
        )

    @staticmethod
    def longtime_hot() -> GitHubPaginatedListView:
        return GitHubPaginatedListView(
            fetcher=lambda : service.get_total_hot(limit=200),
            header="🏆 <b>长期热门项目</b>",
            prefix="github_longtime_hot",
            empty_message="暂无项目"
        )

    @staticmethod
    def trend():
        return GitHubPaginatedListView(
            fetcher=lambda: service.get_trend(),
            header="📈 <b>Trending 趋势</b>",
            prefix="github_trending_now",
            empty_message="暂无项目"
        )

    @staticmethod
    def by_language(language: str) -> GitHubPaginatedListView:
        return GitHubPaginatedListView(
            fetcher=lambda: service.get_by_language(language.upper(), limit=1000),
            header=f"<b>{escape(language.upper())} 项目</b>",
            prefix=f"github_lang_{language.lower()}",
            empty_message=f"{language.upper()} 暂无项目"
        )

    @staticmethod
    def by_tag(tag: str) -> GitHubPaginatedListView:
        return GitHubPaginatedListView(
            fetcher=lambda: service.get_by_tag(tag.lower(), limit=1000),
            header=f"<b>{escape(tag.upper())} 项目</b>",
            prefix=f"github_tag_{tag.lower()}",
            empty_message=f"{tag.upper()} 暂无项目"
        )

    @staticmethod
    def tag_ai() -> GitHubPaginatedListView:
        return GitHubPaginatedListView(
            fetcher=lambda: service.get_by_tag("AI", limit=1000),
            header="🌐 <b>AI / 大模型项目</b>",
            prefix="github_tag_ai",
            empty_message="暂无 AI 项目"
        )

    @staticmethod
    def my_favorites(telegram_id: int) -> GitHubPaginatedListView:
        return GitHubPaginatedListView(
            fetcher=lambda: service.get_user_favorites(telegram_id, limit=1000),
            header="❤️ <b>我的收藏</b>",
            prefix="github_my_favorites",
            empty_message="你还没有收藏任何项目，浏览项目时可以点击收藏按钮添加"
        )

    @staticmethod
    def search(keyword: str, search_id: str) -> GitHubPaginatedListView:
        return GitHubPaginatedListView(
            fetcher=lambda: service.search_projects(keyword, limit=1000),
            header=f"🔍 <b>'{escape(keyword)}' 搜索结果</b>",
            prefix=f"github_search_{search_id}",
            empty_message=f"未找到关于 '{keyword}' 的项目"
        )

    @staticmethod
    async def get_page_views(prefix: str, query, page):
        if prefix == "github_recent_hot":
            await GitHubProjectViews.recent_hot().render(query.message, page=page)
        elif prefix == "github_longtime_hot":
            await GitHubProjectViews.longtime_hot().render(query.message, page=page)
        elif prefix == "github_trending_now":
            await GitHubProjectViews.trend().render(query.message, page=page)
        elif prefix == "github_tag_ai":
            await GitHubProjectViews.tag_ai().render(query.message, page=page)
        elif prefix == "github_my_favorites":
            await GitHubProjectViews.my_favorites(query.from_user.id).render(query.message, page=page)


_search_cache: dict[str, str] = {}
def _make_search_id(telegram_id: int) -> str:
    return str(telegram_id)


@router.message(Command("github_start"))
async def cmd_start(message: Message):
    welcome_text = (
        "<b>🚀 AI 项目探索 Bot</b>\n"
        "在这里发现最有潜力的开源项目\n"
        "每天为你推荐优质、trending 的 GitHub 项目\n"
        "推荐HuggingFace有潜力的AI模型，以及相关新闻"
        "选择下方分类开始探索吧 ↓"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=GitHubKeyboards.main_menu())



@router.message(Command("github_recent_hot"))
@router.callback_query(F.data == "github_recent_hot")
async def recent_hot(event:  Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await GitHubProjectViews.recent_hot().render(target, page=0)

@router.message(Command("github_longtime_hot"))
@router.callback_query(F.data == "github_longtime_hot")
async def longtime_hot(event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 加载中...")
        target = event.message
    else:
        target = event
    await GitHubProjectViews.longtime_hot().render(target, page=0)


@router.callback_query(F.data.startswith("github_lang_"))
async def by_language(query: CallbackQuery):
    if not query.data: return
    await query.answer("🔄 加载中...")
    base = query.data.split(":")[0]
    language = base.removeprefix("github_lang_")
    page = _extract_page(query.data)
    await GitHubProjectViews.by_language(language).render(query.message, page=page)

@router.callback_query(F.data.startswith("github_tag_"))
async def by_tag(query: CallbackQuery):
    if not query.data: return
    await query.answer("🔄 加载中...")
    base = query.data.split(":")[0]
    tag = base.removeprefix("github_tag_")
    page = _extract_page(query.data)
    await GitHubProjectViews.by_tag(tag).render(query.message, page=page)


@router.message(Command("github_trending_now"))
@router.callback_query(F.data == "github_trending_now")
async def trending_now(query: Union[Message, CallbackQuery]):
    if isinstance(query, CallbackQuery):
        await query.answer("🔄 加载中...")
        target = query.message
    else:
        target = query
    await GitHubProjectViews.trend().render(target, page=0)

@router.message(Command("github_my_favorites"))
@router.callback_query(F.data == "github_my_favorites")
async def my_favorites(query: Union[Message, CallbackQuery]):
    if isinstance(query, CallbackQuery):
        await query.answer("🔄 加载中...")
        target = query.message
    else:
        target = query
    await GitHubProjectViews.my_favorites(query.from_user.id).render(target, page=0)


@router.message(Command("github_search"))
@router.callback_query(F.data == "github_search")
async def search_start(event: Union[Message, CallbackQuery]):
    prompt = "🔍 请输入搜索关键词：\n例如：Python、AI、Django 等"
    if isinstance(event, CallbackQuery):
        await event.answer()
        set_pending(event.from_user.id, "github")
        await event.message.edit_text(text=prompt, reply_markup=GitHubKeyboards.back_only())
    else:
        set_pending(event.from_user.id, "github")
        await event.answer(text=prompt, reply_markup=GitHubKeyboards.back_only())

@router.message(F.text, is_pending("github"))
async def search_projects(message: Message):
    clear_pending(message.from_user.id)
    keyword = message.text.strip()
    if len(keyword) < 2:
        await message.reply("⚠️ 搜索词至少 2 个字符")
        return

    search_id = _make_search_id(message.from_user.id)
    _search_cache[search_id] = keyword
    view = GitHubProjectViews.search(keyword, search_id)
    await view.render(message, page=0, edit=False)


@router.callback_query(F.data.startswith("github_search_"))
async def search_paginate(query: CallbackQuery):
    if not query.data: return
    await query.answer("🔄 加载中...")
    base = query.data.split(":")[0]
    search_id = base.split("_", 1)[1]
    keyword = _search_cache.get(search_id)

    if not keyword:
        await query.message.edit_text(
            text="⚠️ 搜索会话已过期，请重新输入关键词搜索",
            reply_markup=GitHubKeyboards.back_only()
        )
        return
    page = _extract_page(query.data)
    await GitHubProjectViews.search(keyword, search_id).render(query.message, page=page)



@router.callback_query(F.data == "github_back_menu")
async def back_menu(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "👋 欢迎使用 <b>Github 项目推荐 Bot</b>！\n\n选择你感兴趣的内容：",
        parse_mode="HTML",
        reply_markup=GitHubKeyboards.main_menu()
    )

@router.callback_query(F.data == "github_more_categories")
async def more_categories(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "📁 <b>更多分类</b>\n\n选择你感兴趣的编程语言或领域：",
        parse_mode="HTML",
        reply_markup=GitHubKeyboards.get_more_categories_keyboard()
    )

def _extract_page(callback_data: str) -> int:
    parts = callback_data.split(":")
    if len(parts) == 3 and parts[1] == "page":
        try:
            return int(parts[2])
        except ValueError:
            return 0
    return 0

@router.callback_query(F.data.startswith("github_fav_add:"))
async def add_favorite(query: CallbackQuery):
    project_id = int(query.data.split(":")[1])
    result = service.toggle_favorite(
        telegram_id=query.from_user.id,
        project_id=project_id
    )
    if result.get("action") == "removed":
        await query.answer("❤️ 已取消收藏")
    else:
        await query.answer("❤️ 已收藏")


@router.message(Command("github_help"))
async def cmd_help(message: Message):
    help_text = (
        "<b>📖 GitHub 项目推荐 Bot 使用帮助</b>\n\n"

        "<b>🔍 主要功能：</b>\n"
        "• <code>/start</code> - 打开主菜单\n"
        "• <code>/recent_hot</code> - 查看近期热门项目（最近增长快）\n"
        "• <code>/longtime_hot</code> - 查看长期热门项目（Star 较高且稳定）\n"
        "• <code>/trending</code> - 查看 GitHub 当前 Trending 项目\n"
        "• <code>/search</code> - 搜索项目（支持关键词）\n"
        "• <code>/my_favorites</code> - 查看你收藏的项目\n\n"

        "<b>🎯 使用技巧：</b>\n"
        "• 点击主菜单中的按钮即可快速浏览\n"
        "• 支持分页查看（⬅️ 上一页 / 下一页 ➡️）\n"
        "• 项目详情中可点击收藏❤️\n"
        "• 搜索支持多种语言和领域关键词（如：AI、爬虫、Web、工具等）\n\n"

        "<b>🌟 项目数据来源：</b>\n"
        "GitHub 公开数据 + 增长趋势分析\n\n"

        "有任何问题或建议，欢迎随时反馈！"
    )

    help_text += "\n\n<b>快速入口：</b>"
    await message.answer(
        help_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=GitHubKeyboards.main_menu()  # 直接显示主菜单
    )
