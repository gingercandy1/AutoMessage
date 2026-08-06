"""Q4 审查群组 inline 回调（✅/❌）。

仅审查群组内的管理员能操作（telegram_id 白名单待补，目前任何人都可点，
后续可加 ADMIN 校验）。
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery

from app.config import settings
from app.logger import AppLogger
from app.services.review_service import ReviewService, REVIEW_APPROVE, REVIEW_REJECT

logger = AppLogger.get(__name__)
router = Router()

_review = ReviewService()


@router.callback_query(F.data.startswith(f"{REVIEW_APPROVE}:"))
async def on_approve(query: CallbackQuery):
    item_id = int(query.data.split(":", 1)[1])
    await query.answer("✅ 通过")
    ok = await _review.approve(query.bot, item_id, query.message)
    if not ok:
        await query.answer("⚠️ 操作失败", show_alert=True)


@router.callback_query(F.data.startswith(f"{REVIEW_REJECT}:"))
async def on_reject(query: CallbackQuery):
    item_id = int(query.data.split(":", 1)[1])
    await query.answer("❌ 拒绝")
    await _review.reject(query.bot, item_id, query.message)
