import asyncio
from datetime import datetime
from aiogram import Bot

from app.config import settings
from app.services.github_service import GithubService
from app.services.huggingface_service import HuggingFaceService


def _fmt(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class DailyPublisher:
    def __init__(self):
        self.bot = Bot(token=settings.telegram_bot_token)
        self.github_service = GithubService()
        self.hf_service = HuggingFaceService()

    async def publish_to_user(self, telegram_id: int, top_n: int = 5):
        """主动推送当日 GitHub 趋势/热门 + HF 热门/近期更新 TOP N（ADR-0003 bot push）。

        直接读取已采集的 ContentItem.metrics 与已有 Summary，不再重复调用 LLM。
        """
        github_hot = self.github_service.get_recent_hot(day=8)[:top_n]
        github_trend = self.github_service.get_trend(day=8)[:top_n]
        hf_hot = self.hf_service.get_latest_ranking(limit=top_n)
        hf_updated = self.hf_service.get_recently_updated(limit=top_n)

        if not any([github_hot, github_trend, hf_hot, hf_updated]):
            await self.bot.send_message(telegram_id, "今日暂无新内容~")
            return

        today = datetime.now().strftime('%Y-%m-%d')
        await self.bot.send_message(
            telegram_id,
            f"🌅 <b>每日精选推送</b>（{today}）",
            parse_mode="HTML",
        )

        if github_trend:
            await self._send_section(telegram_id, "📈 GitHub 趋势", github_trend, "github")
        if github_hot:
            await self._send_section(telegram_id, "🔥 GitHub 热门", github_hot, "github")
        if hf_hot:
            await self._send_section(telegram_id, "🤗 HF 热门", hf_hot, "huggingface")
        if hf_updated:
            await self._send_section(telegram_id, "🆕 HF 近期更新", hf_updated, "huggingface")

    async def _send_section(self, telegram_id: int, title: str, items, source: str):
        lines = [f"<b>{title}</b>", ""]
        for i, item in enumerate(items, 1):
            m = item.metrics or {}
            if source == "github":
                name = item.title
                url = item.url
                meta = f"⭐ {_fmt(m.get('stars'))}"
                lang = m.get('language')
                if lang:
                    meta += f" · {lang}"
            else:
                model_id = item.external_id
                name = model_id.split("/")[-1]
                url = f"https://huggingface.co/{model_id}"
                meta = f"👍 {_fmt(m.get('likes'))} · ⬇️ {_fmt(m.get('downloads'))}"
            one_liner = ""
            if item.summary and getattr(item.summary, "one_liner", None):
                one_liner = f"\n  {item.summary.one_liner}"
            lines.append(f"{i}. <a href=\"{url}\">{name}</a> — {meta}{one_liner}")
        text = "\n".join(lines)
        await self.bot.send_message(
            telegram_id, text.strip(), parse_mode="HTML", disable_web_page_preview=True
        )
        await asyncio.sleep(0.3)

    async def publish_to_channel(self, channel_id: str):
        # ADR-0001/0003: only reviewed news reaches the channel; wired in Phase 3 (review gate).
        raise NotImplementedError("Channel publishing requires the review gate (Phase 3).")

    async def close(self):
        await self.bot.session.close()