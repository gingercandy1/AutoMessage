"""订阅管理（ADR-0003 bot push）。

用户用 /subscribe 订阅每日主动推送。支持两种粒度：
  - 不带参数：订阅「全部」每日精选（向后兼容，subscription_type='daily_push'）。
  - /subscribe <维度>:<值>：按维度订阅（language/tag/author/source），写入独立的
    Subscription 行；publisher 的 publish_to_user 按活跃维度过滤（见 #09）。

/unsubscribe 取消全部该用户的活跃订阅。
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.database import SessionLocal
from app.database.models import Subscription
from app.logger import AppLogger

logger = AppLogger.get(__name__)
router = Router()

PUSH_TYPE = "daily_push"
PUSH_VALUE = "all"

VALID_DIMENSIONS = {"language", "tag", "author", "source"}


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


def _set_dimension_subscription(telegram_id: int, dim: str, value: str, active: bool) -> bool:
    """写入/更新一个维度订阅行（source/tag/language/author）。"""
    db = SessionLocal()
    try:
        row = (
            db.query(Subscription)
            .filter_by(telegram_id=telegram_id, subscription_type=dim,
                       subscription_value=value)
            .first()
        )
        if row:
            row.is_active = 1 if active else 0
        elif active:
            db.add(Subscription(
                telegram_id=telegram_id,
                subscription_type=dim,
                subscription_value=value,
                is_active=1,
            ))
        else:
            return False
        db.commit()
        return True
    finally:
        db.close()


def _list_subscriptions(telegram_id: int) -> list:
    db = SessionLocal()
    try:
        rows = (
            db.query(Subscription)
            .filter(Subscription.telegram_id == telegram_id,
                    Subscription.is_active == 1)
            .all()
        )
        return [(r.subscription_type, r.subscription_value) for r in rows]
    finally:
        db.close()


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    text = message.text or ""
    # /subscribe language:python  -> 维度订阅
    if " " in text.strip():
        arg = text.strip().split(" ", 1)[1].strip()
        if ":" in arg:
            dim, _, value = arg.partition(":")
            dim, value = dim.strip().lower(), value.strip().lower()
            if dim not in VALID_DIMENSIONS:
                await message.answer(
                    f"⚠️ 未知维度「{dim}」。可用维度："
                    + "、".join(sorted(VALID_DIMENSIONS))
                )
                return
            if not value:
                await message.answer("⚠️ 请指定维度值，例如 /subscribe language:python")
                return
            _set_dimension_subscription(message.from_user.id, dim, value, active=True)
            await message.answer(
                f"✅ 已订阅维度 {dim}:{value}。\n"
                f"每日推送将只发送符合你订阅维度的内容。用 /unsubscribe 取消。"
            )
            return
    # 无参数：订阅全部
    changed = _set_subscription(message.from_user.id, active=True)
    if changed:
        await message.answer(
            "✅ 已订阅每日精选推送（全部内容）！\n"
            "每天 09:00 我会主动给你发送 GitHub 趋势/热门 + HuggingFace 热门/近期更新。\n"
            "也可按维度订阅，例如 /subscribe language:python 或 /subscribe source:huggingface。\n"
            "用 /unsubscribe 可随时取消。"
        )
    else:
        await message.answer("ℹ️ 你已经订阅了每日推送~")


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    tid = message.from_user.id
    db = SessionLocal()
    try:
        rows = db.query(Subscription).filter(
            Subscription.telegram_id == tid, Subscription.is_active == 1
        ).all()
        for r in rows:
            r.is_active = 0
        db.commit()
        changed = len(rows) > 0
    finally:
        db.close()
    if changed:
        await message.answer("🔕 已取消全部每日推送订阅。用 /subscribe 可随时重新开启。")
    else:
        await message.answer("ℹ️ 你当前没有活跃的推送订阅。")


@router.message(Command("mysubs"))
async def cmd_my_subs(message: Message):
    subs = _list_subscriptions(message.from_user.id)
    if not subs:
        await message.answer("ℹ️ 你当前没有活跃的推送订阅。用 /subscribe 开启。")
        return
    lines = ["📋 <b>你的活跃订阅</b>："]
    for stype, sval in subs:
        lines.append(f"· {stype}:{sval}")
    await message.answer("\n".join(lines), parse_mode="HTML")
