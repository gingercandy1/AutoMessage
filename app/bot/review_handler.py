"""Q4 审查 inline 回调（✅/❌）。

审查卡片私聊推送给白名单管理员（REVIEW_ADMIN_IDS），仅白名单内的
telegram_id 能操作；非管理员点击会被拒绝并收到提示。
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery

from app.config import settings
from app.logger import AppLogger
from app.services.review_service import ReviewService, REVIEW_APPROVE, REVIEW_REJECT

logger = AppLogger.get(__name__)
router = Router()

_review = ReviewService()


def _is_review_admin(telegram_id: int) -> bool:
    return telegram_id in settings.review_admin_ids


async def _deny_if_not_admin(query: CallbackQuery) -> bool:
    """非管理员 -> 记录警告并拒绝，返回 False；管理员返回 True。"""
    if _is_review_admin(query.from_user.id):
        return True
    logger.warning(f"非管理员尝试操作审查 item={query.data} user={query.from_user.id}")
    await query.answer("⛔ 无权限，仅管理员可操作", show_alert=True)
    return False


async def _ensure_pending(query: CallbackQuery, item_id: int) -> bool:
    """卡片已被其他管理员处理（非待审）-> 提示并返回 False。"""
    item = _review.news.get_by_id(item_id)
    if item and item.review_status == "pending":
        return True
    await query.answer("该条已被其他管理员处理", show_alert=True)
    return False


@router.callback_query(F.data.startswith(f"{REVIEW_APPROVE}:"))
async def on_approve(query: CallbackQuery):
    if not await _deny_if_not_admin(query):
        return
    item_id = int(query.data.split(":", 1)[1])
    if not await _ensure_pending(query, item_id):
        return
    await query.answer("✅ 通过")
    ok = await _review.approve(query.bot, item_id, query.message)
    if not ok:
        await query.answer("⚠️ 操作失败", show_alert=True)
    else:
        logger.info(f"管理员 {query.from_user.id} 通过 review item={item_id}")


@router.callback_query(F.data.startswith(f"{REVIEW_REJECT}:"))
async def on_reject(query: CallbackQuery):
    if not await _deny_if_not_admin(query):
        return
    item_id = int(query.data.split(":", 1)[1])
    if not await _ensure_pending(query, item_id):
        return
    await query.answer("❌ 拒绝")
    ok = await _review.reject(query.bot, item_id, query.message)
    if not ok:
        await query.answer("⚠️ 操作失败", show_alert=True)
    else:
        logger.info(f"管理员 {query.from_user.id} 拒绝 review item={item_id}")