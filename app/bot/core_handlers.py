from typing import Union

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import BotCommand
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

from app.bot.github_handler import GitHubProjectViews, GitHubKeyboards
from app.bot.huggingface_handler import HuggingfaceProjectViews, HuggingfaceKeyboards
from app.bot.news_handler import NewsProjectViews
from app.logger import AppLogger

logger = AppLogger.get(__name__)
router = Router()
PAGE_SIZE = 6


class MainKeyboards:
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🐙 GitHub 项目", callback_data="nav_github"),
                InlineKeyboardButton(text="🤗 HuggingFace 模型", callback_data="nav_huggingface"),
            ],
            [
                InlineKeyboardButton(text="📰 AI/科技新闻", callback_data="nav_news"),
            ],
            [InlineKeyboardButton(text="🔙 返回菜单", callback_data="back_menu")],
        ])

    @staticmethod
    def back_only() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回菜单", callback_data="back_menu")]
        ])


BOT_COMMANDS = [
    BotCommand(command="start", description="🚀 打开主菜单"),
    BotCommand(command="github_start", description="🐙 查看 GitHub 项目"),
    BotCommand(command="huggingface_start", description="🤗 查看 HuggingFace 模型"),
    BotCommand(command="news_start", description="📰 查看 AI/科技新闻"),
    BotCommand(command="subscribe", description="🔔 订阅每日推送"),
    BotCommand(command="unsubscribe", description="🔕 取消每日推送"),
    BotCommand(command="help", description="❓ 查看使用帮助"),
]


@router.callback_query(F.data.contains(":page:"))
async def generic_paginate(query: CallbackQuery):
    """
    统一的翻页入口。根据 callback_data 前缀，把请求分发给对应模块的分页视图。
    例如 "huggingface_recent_hot:page:1" -> HuggingfaceProjectViews
         "github_recent_hot:page:1"      -> GitHubProjectViews
         "news_recent_hot:page:1"        -> NewsProjectViews
    """
    if not query.data:
        return
    await query.answer("🔄 加载中...")
    prefix = query.data.split(":")[0]
    page = _extract_page(query.data)
    if prefix.startswith("huggingface"):
        await HuggingfaceProjectViews.get_page_views(prefix, query=query, page=page)
    elif prefix.startswith("news"):
        await NewsProjectViews.get_page_views(prefix, query=query, page=page)
    else:
        await GitHubProjectViews.get_page_views(prefix, query=query, page=page)


def _extract_page(callback_data: str) -> int:
    parts = callback_data.split(":")
    if len(parts) == 3 and parts[1] == "page":
        try:
            return int(parts[2])
        except ValueError:
            return 0
    return 0


@router.message(Command("start"))
async def cmd_start(message: Message):
    welcome_text = (
        "<b>🚀 AI 项目探索 Bot</b>\n"
        "在这里发现最有潜力的开源项目\n"
        "每天为你推荐优质、trending 的 GitHub 项目\n"
        "以及有潜力的 HuggingFace AI 模型\n"
        "还有精选的 AI/科技新闻资讯\n"
        "选择下方分类开始探索吧 ↓"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=MainKeyboards.main_menu())


@router.callback_query(F.data == "nav_github")
async def nav_github(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "🐙 <b>GitHub 项目推荐</b>\n\n选择你感兴趣的内容：",
        parse_mode="HTML",
        reply_markup=GitHubKeyboards.main_menu(),
    )


@router.callback_query(F.data == "nav_huggingface")
async def nav_huggingface(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "🤗 <b>HuggingFace 模型推荐</b>\n\n选择你感兴趣的分类：",
        parse_mode="HTML",
        reply_markup=HuggingfaceKeyboards.main_menu(),
    )


@router.callback_query(F.data == "nav_news")
async def nav_news(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "📰 <b>AI/科技新闻聚合</b>\n\n选择你感兴趣的内容：",
        parse_mode="HTML",
        reply_markup=NewsProjectViews.main_menu(),
    )


@router.callback_query(F.data == "back_menu")
async def back_menu(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "👋 欢迎使用 <b>AI 项目推荐 Bot</b>！\n\n选择你感兴趣的内容：",
        parse_mode="HTML",
        reply_markup=MainKeyboards.main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "<b>📖 AI 项目推荐 Bot 使用帮助</b>\n\n"
        "<b>🔍 主要功能：</b>\n"
        "• <code>/start</code> - 打开主菜单\n\n"

        "<b>🐙 GitHub 项目：</b>\n"
        "• <code>/github_start</code> - 打开 GitHub 菜单\n"
        "• <code>/github_recent_hot</code> - 近期热门项目\n"
        "• <code>/github_longtime_hot</code> - 长期热门项目（Star 较高且稳定）\n"
        "• <code>/github_trending_now</code> - GitHub 当前 Trending 项目\n"
        "• <code>/github_search</code> - 搜索项目（支持关键词）\n"
        "• <code>/github_my_favorites</code> - 查看你收藏的项目\n\n"

        "<b>🤗 HuggingFace 模型：</b>\n"
        "• <code>/huggingface_start</code> - 打开 HuggingFace 菜单\n"
        "• <code>/huggingface_recent_hot</code> - 近期热门模型\n"
        "• <code>/huggingface_longtime_hot</code> - 长期热门模型（按点赞数）\n"
        "• <code>/huggingface_trending_now</code> - 当前 Trending 模型\n"
        "• <code>/huggingface_search</code> - 搜索模型（支持关键词）\n"
        "• <code>/huggingface_my_favorites</code> - 查看你收藏的模型\n\n"

        "<b>📰 AI/科技新闻：</b>\n"
        "• <code>/news_start</code> - 打开新闻菜单\n"
        "• <code>/news_recent_hot</code> - 近期热门新闻\n"
        "• <code>/news_trending_now</code> - 趋势新闻\n"
        "• <code>/news_hacker_news</code> - Hacker News 热门\n"
        "• <code>/news_search</code> - 搜索新闻（支持关键词）\n"
        "• <code>/news_my_favorites</code> - 查看你收藏的新闻\n\n"

        "<b>🎯 使用技巧：</b>\n"
        "• 点击菜单按钮即可快速浏览，无需记命令\n"
        "• 支持分页查看（⬅️ 上一页 / 下一页 ➡️）\n"
        "• 项目/模型/新闻详情中可点击收藏❤️\n"
        "• 搜索支持多种语言和领域关键词（如：AI、爬虫、Web、llama、RAG、transformer 等）\n\n"

        "有任何问题或建议，欢迎随时反馈！"
    )
    await message.answer(
        help_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=MainKeyboards.main_menu(),
    )