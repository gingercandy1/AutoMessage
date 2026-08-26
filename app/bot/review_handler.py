"""Q4 审查 inline 回调（✅/❌/✏️）。

审查卡片推送到审查频道（REVIEW_CHAT_ID）或私聊推送给白名单管理员
（REVIEW_ADMIN_IDS，频道模式未配置时的退路）。操作权限始终以
REVIEW_ADMIN_IDS 白名单为准：非白名单 telegram_id 点击会被拒绝并提示。

✏️ 编辑流程（两种方式等价）：
A. 直接编辑：在 Telegram 里长按卡片 ->「编辑消息」，修改 📌 一句话或 🔹 正文段，
   保存后 bot 自动反解并同步数据库、重绘规范卡片；
B. 回复即改：点击 ✏️ 后回复卡片发送修改稿（一句话与正文用单独一行 `---` 分隔）。
发送 `取消` 可退出 B 的编辑状态。
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.logger import AppLogger
from app.services.review_service import (
    ReviewService,
    REVIEW_APPROVE,
    REVIEW_REJECT,
    REVIEW_EDIT,
    _review_keyboard,
    _render_review_card,
    _parse_card_edit,
    card_registry,
)

logger = AppLogger.get(__name__)
router = Router()

_review = ReviewService()

#: user_id -> {"item_id": int, "chat_id": int, "message_id": int}
_edit_state: dict[int, dict] = {}

EDIT_CANCEL_WORDS = {"取消", "cancel", "Cancel", "CANCEL"}


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


@router.callback_query(F.data.startswith(f"{REVIEW_EDIT}:"))
async def on_edit(query: CallbackQuery):
    if not await _deny_if_not_admin(query):
        return
    item_id = int(query.data.split(":", 1)[1])
    if not await _ensure_pending(query, item_id):
        return
    _edit_state[query.from_user.id] = {
        "item_id": item_id,
        "chat_id": query.message.chat.id,
        "message_id": query.message.message_id,
    }
    await query.answer()
    await query.bot.send_message(
        query.message.chat.id,
        "✏️ 两种编辑方式（等价）：\n\n"
        "1️⃣ <b>直接编辑本卡片</b>：长按卡片 → 编辑消息，"
        "改 📌 一句话或 🔹 正文段，保存即自动同步\n\n"
        "2️⃣ <b>回复卡片发修改稿</b>：第一行 = 一句话简介\n"
        "<code>---</code>\n"
        "其后 = 正文；不带 <code>---</code> 则只改正文\n\n"
        "发送「取消」退出方式 2 的编辑状态。",
        parse_mode="HTML",
    )
    logger.info(f"管理员 {query.from_user.id} 进入编辑 item={item_id}")


def is_editing_reply(message: Message) -> bool:
    """该消息是否为「处于编辑态的管理员对目标卡片的回复」。"""
    user = getattr(message, "from_user", None)
    reply = getattr(message, "reply_to_message", None)
    if not user or not reply:
        return False
    st = _edit_state.get(user.id)
    if not st:
        return False
    chat = getattr(message, "chat", None)
    return (reply.message_id == st["message_id"]
            and chat is not None and chat.id == st["chat_id"])


@router.message(F.text, is_editing_reply)
async def on_edit_reply(message: Message):
    st = _edit_state.pop(message.from_user.id, {})
    item_id = st.get("item_id")

    text = (message.text or "").strip()
    if text in EDIT_CANCEL_WORDS:
        await message.reply("已退出编辑")
        return

    from app.services.review_service import parse_edit_text
    one_liner, summary = parse_edit_text(text)
    if one_liner is None and summary is None:
        await message.reply("⚠️ 内容为空，已退出编辑")
        return
    if not _review.update_summary(item_id, one_liner=one_liner, summary=summary):
        await message.reply("⚠️ 更新失败，请重试或联系管理员检查日志")
        return

    # 原地重绘卡片（保留审查按钮）
    item = _review.news.get_by_id(item_id)
    try:
        await message.bot.edit_message_text(
            chat_id=st["chat_id"],
            message_id=st["message_id"],
            text=_render_review_card(item),
            parse_mode="HTML",
            reply_markup=_review_keyboard(item_id),
        )
        await message.reply("✏️ 已更新")
        logger.info(f"管理员 {message.from_user.id} 编辑 review item={item_id}")
    except Exception as e:
        logger.error(f"编辑后重绘卡片失败 item={item_id}: {e}")
        await message.reply("✏️ 内容已更新，但卡片刷新失败（可能已被删除）")


def _card_edit_changed(item_id: int, one_liner, summary) -> bool:
    """解析结果与库内现值是否不同（防止 bot 重绘触发的事件造成死循环）。"""
    item = _review.news.get_by_id(item_id)
    s = getattr(item, "summary", None) if item else None
    cur_one = str(getattr(s, "one_liner", "") or "").strip()
    cur_body = str(getattr(s, "summary", "") or "").strip()
    return (one_liner or "").strip() != cur_one or (summary or "").strip() != cur_body


async def _sync_card_edit(event) -> None:
    """管理员直接编辑卡片消息 -> 反解内容同步 Summary 并重绘规范卡片。"""
    key = (event.chat.id, event.message_id)
    item_id = card_registry.get(key)
    if item_id is None:
        return
    # 频道里 Telegram 本就只允许管理员改帖；私聊则按白名单校验
    if event.chat.type == "private" and not _is_review_admin(
            getattr(event, "from_user", None).id if getattr(event, "from_user", None) else 0):
        logger.warning(f"非白名单用户尝试直编卡片 item={item_id}")
        return
    item = _review.news.get_by_id(item_id)
    if not item or item.review_status != "pending":
        return

    one_liner, summary = _parse_card_edit(event.text or "")
    if one_liner is None and summary is None:
        await event.reply("⚠️ 未识别到 📌 / 🔹 段落，本次修改未同步")
        return
    if not _card_edit_changed(item_id, one_liner, summary):
        return  # 内容没变（多半是 bot 自己重绘触发的回声），跳过
    if not _review.update_summary(item_id, one_liner=one_liner, summary=summary):
        await event.reply("⚠️ 同步数据库失败，请查看日志")
        return
    try:
        await event.edit_text(
            _render_review_card(_review.news.get_by_id(item_id)),
            parse_mode="HTML",
            reply_markup=_review_keyboard(item_id),
            disable_web_page_preview=False,
        )
    except Exception as e:
        logger.error(f"直编后重绘卡片失败 item={item_id}: {e}")
    logger.info(f"管理员直编卡片已同步 review item={item_id}")


@router.edited_message(F.text)
async def on_card_edited_private(event: Message):
    await _sync_card_edit(event)


@router.edited_channel_post(F.text)
async def on_card_edited_channel(event):
    await _sync_card_edit(event)
