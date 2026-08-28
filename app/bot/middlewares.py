from typing import Callable, Dict, Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery, Message
from redis.exceptions import RedisError
from app.logger import AppLogger
from app.redis_client import get_redis

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
    """ 通用节流：同一用户两次操作间隔小于 min_interval 秒就拦截。
    用 Redis SET NX PX 做分布式锁，跨实例/重启一致（不再是仅本进程内存态）。"""

    def __init__(self, min_interval: float = 0.8):
        self.min_interval = min_interval
        self.redis = get_redis()
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

        key = f"throttle:{user.id}"
        try:
            allowed = await self.redis.set(key, "1", px=int(self.min_interval * 1000), nx=True)
        except RedisError as e:
            # Redis 抖动/认证失败时，限流是锦上添花的功能，不该因为它挂了就
            # 让所有消息都处理不了——fail-open：放行 + 记警告，而不是让异常
            # 一路冒到 aiogram 的 error middleware（那样等于每条消息都报错）。
            logger.warning(f"限流 Redis 调用失败，fail-open 放行本次: {e}")
            return await handler(event, data)
        if not allowed:
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ 操作太快，请稍候", show_alert=False)
            logger.warning(f"用户 {user.id} 触发通用限流（间隔 < {self.min_interval}s）")
            return None

        return await handler(event, data)


class ToolRateLimitMiddleware(BaseMiddleware):
    """ 针对高成本工具调用的独立限流。用 Redis INCR + EXPIRE 做固定窗口计数，
    跨实例/重启一致（不再是仅本进程内存态）。"""

    def __init__(self, max_calls: int = 5, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.redis = get_redis()
        super().__init__()

    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]) -> Any:
        if not isinstance(event, CallbackQuery) or not event.data.startswith("tool_"):
            return await handler(event, data)

        user = data.get("event_from_user")
        tool_key = event.data.split("_", 1)[1]
        key = f"toolrate:{user.id}:{tool_key}"

        try:
            count = await self.redis.incr(key)
            if count == 1:
                await self.redis.expire(key, self.window_seconds)
        except RedisError as e:
            logger.warning(f"工具限流 Redis 调用失败，fail-open 放行本次: {e}")
            return await handler(event, data)
        if count > self.max_calls:
            await event.answer(
                f"⏳ 该功能请求过于频繁，请 {self.window_seconds} 秒后再试",
                show_alert=True
            )
            logger.warning(f"用户 {user.id} 触发工具限流: {tool_key}")
            return None

        return await handler(event, data)
