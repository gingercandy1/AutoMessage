import asyncio
from datetime import datetime
from aiogram import Bot

from app.config import settings
from app.services.github_service import GithubService
from app.ai.summarizer import ProjectSummarizer


class DailyPublisher:
    def __init__(self):
        self.bot = Bot(token=settings.telegram_bot_token)
        self.github_service = GithubService()
        self.summarizer = ProjectSummarizer(provider=settings.default_llm_provider)

    async def publish_to_user(self, telegram_id: int):
        projects = self.github_service.get_recent_hot()
        if not projects:
            await self.bot.send_message(telegram_id, "今日暂无推荐项目~")
            return

        await self.bot.send_message(
            telegram_id,
            f"🌅 <b>每日精选项目推荐</b>（{datetime.now().strftime('%Y-%m-%d')}）\n\n"
            "以下是今天为你挑选的潜力项目：",
            parse_mode="HTML"
        )

        for project in projects:
            summary = await self.summarizer.summarize(project.title, project.summary or "")
            text = f"""
                    <b>{project.title}</b>
                    ⭐ {project.stargazers_count:,} stars • {project.language}
                    {summary.get('one_liner', project.summary or '')}
                    🔗 <a href="{project.url}">前往 GitHub</a>
                    """
            await self.bot.send_message(
                telegram_id,
                text.strip(),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            await asyncio.sleep(0.5)    

    async def publish_to_channel(self, channel_id: str):\
        pass

    async def close(self):
        await self.bot.session.close()