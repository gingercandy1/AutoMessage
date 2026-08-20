import asyncio
import json
from datetime import datetime
from aiogram import Bot

from app.config import settings
from app.logger import AppLogger
from app.services.github_service import GithubService
from app.services.huggingface_service import HuggingFaceService
from app.services.review_service import _render_review_card
from app.services.news_service import NewsService
from app.database import SessionLocal
from app.database.models import NewsCluster
from app.database.models import Subscription

logger = AppLogger.get(__name__)


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

    def _user_subscriptions(self, telegram_id: int) -> list:
        """返回该用户的活跃订阅维度列表 [(type, value), ...]。
        空列表表示未做维度订阅（向后兼容：推送全部）。"""
        db = SessionLocal()
        try:
            rows = (
                db.query(Subscription)
                .filter(Subscription.telegram_id == telegram_id,
                        Subscription.is_active == 1)
                .all()
            )
            return [(r.subscription_type, (r.subscription_value or "").lower())
                    for r in rows]
        finally:
            db.close()

    @staticmethod
    def _item_matches(item, subs: list) -> bool:
        """item 是否命中订阅者任一维度。
        subs 为空 -> 命中全部（兼容旧订阅）。"""
        if not subs:
            return True
        m = item.metrics or {}
        title = (item.title or "").lower()
        tags = []
        if item.summary and getattr(item.summary, "tags", None):
            raw = item.summary.tags
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    raw = []
            if isinstance(raw, list):
                tags = [str(t).lower() for t in raw]
        source = (item.source or "").lower()
        for stype, sval in subs:
            if stype == "source" and source == sval:
                return True
            if stype == "language" and (m.get("language") or "").lower() == sval:
                return True
            if stype == "tag" and sval in tags:
                return True
            if stype == "author" and sval in title:
                return True
        return False

    def _filter(self, items, subs: list):
        return [it for it in items if self._item_matches(it, subs)]

    async def publish_to_user(self, telegram_id: int, top_n: int = 5):
        """主动推送当日 GitHub 趋势/热门 + HF 热门/近期更新 TOP N（ADR-0003 bot push）。

        直接读取已采集的 ContentItem.metrics 与已有 Summary，不再重复调用 LLM。
        #09：按订阅者的活跃订阅维度过滤，只推其关注的内容。
        """
        subs = self._user_subscriptions(telegram_id)
        github_hot = self._filter(self.github_service.get_recent_hot(day=8)[:top_n], subs)
        github_trend = self._filter(self.github_service.get_trend(day=8)[:top_n], subs)
        hf_hot = self._filter(self.hf_service.get_latest_ranking(limit=top_n), subs)
        hf_updated = self._filter(self.hf_service.get_recently_updated(limit=top_n), subs)

        if not any([github_hot, github_trend, hf_hot, hf_updated]):
            # 仍告知订阅者今日无命中内容，避免静默
            await self.bot.send_message(telegram_id, "🌅 今日没有符合你订阅维度的新内容~")
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

    async def publish_to_channel(self, channel_id: int | None = None):
        """将已审查通过(approved)的新闻转发到公开频道。
        仅在人工审查通过后推送；未经审查或拒绝的绝不进频道。
        channel_id 缺省读 settings.public_channel_id。
        """
        target = channel_id or settings.public_channel_id
        if not target:
            logger.warning("public_channel_id 未配置，跳过频道发布")
            return 0

        news = NewsService()
        approved = news.get_approved_news(limit=50)
        if not approved:
            return 0
        # Q10 跨运行去重：若某条已批准新闻是某个 NewsCluster 的非 canonical 成员，
        # 说明同一故事已由 canonical 代表并推送过（或将在本次推送），跳过以免重复。
        db = SessionLocal()
        try:
            member_to_canonical = {}
            for c in db.query(NewsCluster).all():
                members = c.member_item_ids or []
                for m in members:
                    if m != c.canonical_item_id:
                        member_to_canonical[m] = c.canonical_item_id
        finally:
            db.close()

        sent = 0
        for item in approved:
            if item.id in member_to_canonical:
                logger.info(f"⏩ 跳过重复新闻（同簇 canonical={member_to_canonical[item.id]}）: {item.id}")
                continue
            try:
                await self.bot.send_message(
                    target, _render_review_card(item),
                    parse_mode="HTML", disable_web_page_preview=False,
                )
                sent += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"频道发布失败 item={item.id}: {e}")
        logger.info(f"📣 频道发布 {sent} 条已审查新闻")
        return sent

    async def close(self):
        await self.bot.session.close()