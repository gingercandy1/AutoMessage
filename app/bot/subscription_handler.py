"""订阅管理（ADR-0003 bot push）。

用户用 /subscribe 订阅每日主动推送，/unsubscribe 取消。
订阅写入 Subscription 表（subscription_type='daily_push'），scheduler 的
daily_push job 读取后主动推送。
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from app.database import SessionLocal
from app.database.models import Subscription
from app.logger import AppLogger

logger = AppLogger.get(__name__)
router = Router()

PUSH_TYPE = "daily_push"
PUSH_VALUE = "all"


def _set_subscription(telegram_id: int, active: bool) -> bool:
    db = SessionLocal()
    try:
        existing = (
            db.query(Subscription)
            .filter_by(telegram_id=telegram_id, subscription_type=PUSH_TYPE)
            .first()
        )
        if existing:
            existing.is_active = 1 if active else 0
            changed = True
        elif active:
            db.add(Subscription(
                telegram_id=telegram_id,
                subscription_type=PUSH_TYPE,
                subscription_value=PUSH_VALUE,
                is_active=1,
            ))
            changed = True
        else:
            changed = False  # already not subscribed
        db.commit()
        return changed
    finally:
        db.close()


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    changed = _set_subscription(message.from_user.id, active=True)
    if changed:
        await message.answer(
            "✅ 已订阅每日精选推送！\n"
            "每天 09:00 我会主动给你发送 GitHub 趋势/热门 + HuggingFace 热门/近期更新。\n"
            "用 /unsubscribe 可随时取消。"
        )
    else:
        await message.answer("ℹ️ 你已经订阅了每日推送~")


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    changed = _set_subscription(message.from_user.id, active=False)
    if changed:
        await message.answer("🔕 已取消每日推送订阅。用 /subscribe 可随时重新开启。")
    else:
        await message.answer("ℹ️ 你当前没有订阅每日推送。")
