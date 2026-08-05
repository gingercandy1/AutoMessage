import time
from collections import defaultdict
from typing import Callable, Dict, Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery, Message
from app.logger import AppLogger

logger = AppLogger.get(__name__)


from datetime import datetime, UTC
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import SessionLocal
from app.database.models import User


class EnsureUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user:
            self._ensure_user(tg_user)
        return await handler(event, data)

    @staticmethod
    def _ensure_user(tg_user: TgUser):
        db = SessionLocal()
        try:
            stmt = pg_insert(User).values(
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                last_active=datetime.now(UTC),
            ).on_conflict_do_update(
                index_elements=["telegram_id"],
                set_={
                    "username": tg_user.username,
                    "first_name": tg_user.first_name,
                    "last_name": tg_user.last_name,
                    "last_active": datetime.now(UTC),
                },
            )
            db.execute(stmt)
            db.commit()
        finally:
            db.close()

class ThrottlingMiddleware(BaseMiddleware):
    """ 通用节流：同一用户两次操作间隔小于 min_interval 秒就拦截。"""

    def __init__(self, min_interval: float = 0.8):
        self.min_interval = min_interval
        self._last_action: Dict[int, float] = defaultdict(float)
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        last = self._last_action[user.id]
        if now - last < self.min_interval:
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ 操作太快，请稍候", show_alert=False)
            logger.warning(f"用户 {user.id} 触发通用限流（间隔 {now - last:.2f}s）")
            return None

        self._last_action[user.id] = now
        return await handler(event, data)


class ToolRateLimitMiddleware(BaseMiddleware):
    """ 针对高成本工具调用的独立限流。"""

    def __init__(self, max_calls: int = 5, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: Dict[tuple, list] = defaultdict(list)
        super().__init__()

    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]) -> Any:
        if not isinstance(event, CallbackQuery) or not event.data.startswith("tool_"):
            return await handler(event, data)

        user = data.get("event_from_user")
        tool_key = event.data.split("_", 1)[1]
        key = (user.id, tool_key)
        now = time.monotonic()

        # 清理窗口外的记录
        self._calls[key] = [t for t in self._calls[key] if now - t < self.window_seconds]
        if len(self._calls[key]) >= self.max_calls:
            await event.answer(
                f"⏳ 该功能请求过于频繁，请 {self.window_seconds} 秒后再试",
                show_alert=True
            )
            logger.warning(f"用户 {user.id} 触发工具限流: {tool_key}")
            return None

        self._calls[key].append(now)
        return await handler(event, data)
