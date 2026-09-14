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
    BotCommand(command="status", description="📊 查看采集管线健康状态"),
    BotCommand(command="watchdog", description="🐶 看门狗自检与告警历史"),
    BotCommand(command="llm_start", description="🤖 查看 LLM 排行榜"),
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
        "🚀 <b>AI 项目探索 Bot</b>\n"
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


@router.message(Command("status"))
async def cmd_status(message: Message):
    """📊 Q7 管线健康状态：各源成功率 + 条目数 + 最近快照时间。"""
    from app.health import get_pipeline_health

    h = get_pipeline_health()
    if h.get("error"):
        await message.answer(f"⚠️ 健康查询失败：{h['error']}")
        return

    lines = [
        f"📊 <b>采集管线状态</b> · {h['generated_at']}",
        f"总体成功率：<b>{h['overall_success_rate_pct']}%</b> · 条目总数：{h['total_items']}",
        "",
    ]
    for source, s in sorted(h.get("per_source", {}).items()):
        last = s.get("last_snapshot_at") or "—"
        lines.append(
            f"• <b>{source}</b>：{s['total']} 条 "
            f"(✅{s['ok']} ❌{s['error']} ⏳{s['unprocessed']}) "
            f"快照 {last}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("watchdog"))
async def cmd_watchdog(message: Message):
    """🐶 看门狗：实时自检 + 可追溯告警历史（支持问题定位）。"""
    # 仅管理员可触发全量自检，避免被滥用刷爆 GitHub 配额
    from app.config import settings
    from app.watchdog.monitor import get_watchdog_status
    from app.watchdog.alerts import AlertService

    is_admin = not settings.review_admin_ids or message.from_user.id in settings.review_admin_ids
    if not is_admin:
        await message.answer("⛔ 仅管理员可执行看门狗自检")
        return

    await message.answer("🐶 看门狗自检中…（检查 Token/管线/任务新鲜度）")
    try:
        status = await get_watchdog_status(force=True)
        lines = [
            f"🐶 <b>看门狗报告</b> · {status['checked_at']}",
            f"结果：✅{status['ok']} 🟡{status['warning']} 🔴{status['critical']} / {status['total']} 项  告警已发 {status.get('alerts_sent',0)} 条",
            "",
        ]
        for r in status["results"]:
            icon = {"ok": "✅", "warning": "🟡", "critical": "🔴"}.get(r["level"], "❓")
            lines.append(f"{icon} <b>{r['check']}</b> [{r['level']}]")
            lines.append(f"  {r['message']}")
            if r["suggestion"]:
                lines.append(f"  💡 {r['suggestion']}")
            if r["level"] != "ok" and r["details"]:
                detail_preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(r["details"].items())[:3])
                lines.append(f"  🔍 {detail_preview}")
            lines.append("")
        events = AlertService.recent_events(limit=5)
        if events:
            lines.append("<b>最近告警历史：</b>")
            for e in events:
                state = "已恢复" if e["resolved"] else "待处理"
                lines.append(f"• {e['check']} [{e['level']}] {e['message'][:50]}… ({state} {e['created_at'][:16]})")
        # 分段发送，避免超长
        text = "\n".join(lines)
        if len(text) > 3500:
            for chunk in [text[i:i+3500] for i in range(0, len(text), 3500)]:
                await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await message.answer(f"看门狗自检失败：{e}")


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
