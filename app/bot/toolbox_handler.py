import functools
from html import escape
from typing import Callable

from aiogram import Router, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
    LabeledPrice, PreCheckoutQuery
)
from app.logger import AppLogger

logger = AppLogger.get(__name__)
router = Router()

# ============================================================
# 工具定义：每种工具的元信息（名称、单价、callback、说明）
# 价格单位为 Telegram Stars（XTR），1 颗 Star ≈ 0.013 美元（随官方汇率变动）
# ============================================================
class ToolDefinition:
    def __init__(self, key: str, title: str, emoji: str, stars_price: int, description: str):
        self.key = key
        self.title = title
        self.emoji = emoji
        self.stars_price = stars_price
        self.description = description

    @property
    def callback_data(self) -> str:
        return f"tool_{self.key}"

    @property
    def button_text(self) -> str:
        return f"{self.emoji} {self.title}"


TOOLS = {
    "chat": ToolDefinition("chat", "AI 对话", "💬", stars_price=5,
                            description="与 AI 进行多轮对话，支持上下文记忆"),
    "img_gen": ToolDefinition("img_gen", "生成图片", "🎨", stars_price=15,
                               description="根据文字描述生成图片"),
    "video_gen": ToolDefinition("video_gen", "生成视频", "🎬", stars_price=50,
                                 description="根据文字描述生成短视频"),
    "img_edit": ToolDefinition("img_edit", "处理图片", "🖼️", stars_price=10,
                                description="背景移除、修复、风格转换等"),
    "video_edit": ToolDefinition("video_edit", "处理视频", "📹", stars_price=30,
                                  description="剪辑、转码、字幕生成等"),
}


class PremiumService:
    """
    管理用户的付费状态和额度。
    生产环境应该接数据库表，例如：
        user_credits(telegram_id, tool_key, credits_remaining, expires_at)
    这里先用内存字典占位，方便你先跑通流程。
    """

    def __init__(self):
        # {telegram_id: {tool_key: credits_remaining}}
        self._credits: dict[int, dict[str, int]] = {}

    def has_credit(self, telegram_id: int, tool_key: str) -> bool:
        user_credits = self._credits.get(telegram_id, {})
        return user_credits.get(tool_key, 0) > 0

    def consume_credit(self, telegram_id: int, tool_key: str) -> bool:
        if not self.has_credit(telegram_id, tool_key):
            return False
        self._credits[telegram_id][tool_key] -= 1
        return True

    def add_credit(self, telegram_id: int, tool_key: str, amount: int = 1):
        self._credits.setdefault(telegram_id, {})
        self._credits[telegram_id][tool_key] = self._credits[telegram_id].get(tool_key, 0) + amount

    def get_balance(self, telegram_id: int, tool_key: str) -> int:
        return self._credits.get(telegram_id, {}).get(tool_key, 0)

premium_service = PremiumService()


# ============================================================
# 权限校验装饰器
# 用法：在每个工具的 callback handler 上加 @premium_required("img_gen")
# ============================================================
def premium_required(tool_key: str):
    """
    装饰一个 callback_query handler。
    如果用户没有额度，会拦截并弹出购买按钮，不会执行被装饰的函数。
    """
    def decorator(handler: Callable):
        @functools.wraps(handler)
        async def wrapper(query: CallbackQuery, *args, **kwargs):
            telegram_id = query.from_user.id
            if not premium_service.has_credit(telegram_id, tool_key):
                await _prompt_purchase(query, tool_key)
                return
            await handler(query, *args, **kwargs)
        return wrapper
    return decorator


async def _prompt_purchase(query: CallbackQuery, tool_key: str):
    tool = TOOLS[tool_key]
    await query.answer()
    text = (
        f"<b>{tool.emoji} {escape(tool.title)}</b>\n"
        f"{escape(tool.description)}\n\n"
        f"💰 单次使用：<b>{tool.stars_price} ⭐</b>\n\n"
        f"你当前剩余次数：<b>{premium_service.get_balance(query.from_user.id, tool_key)}</b>\n"
        f"购买后即可使用该功能。"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 购买 1 次 ({tool.stars_price}⭐)",
                               callback_data=f"buy_{tool_key}_1")],
        [InlineKeyboardButton(text=f"💳 购买 5 次 ({tool.stars_price * 5}⭐, 省10%)",
                               callback_data=f"buy_{tool_key}_5")],
        [InlineKeyboardButton(text="🔙 返回工具箱", callback_data="toolbox_menu")]
    ])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


class ToolboxKeyboards:
    @staticmethod
    def toolbox_menu() -> InlineKeyboardMarkup:
        rows = []
        # 两个一行排列
        tool_list = list(TOOLS.values())
        for i in range(0, len(tool_list), 2):
            row = [InlineKeyboardButton(text=t.button_text, callback_data=t.callback_data)
                   for t in tool_list[i:i + 2]]
            rows.append(row)
        rows.append([InlineKeyboardButton(text="🔙 返回主菜单", callback_data="back_menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)


def add_toolbox_entry(main_menu_markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """
    在已有主菜单基础上追加"AI 工具箱"入口。
    用法：在你原来的 main_menu() 末尾 return 之前调用，
    或者直接在 main_menu() 里加这一行按钮（见下方示例）。
    """
    main_menu_markup.inline_keyboard.append(
        [InlineKeyboardButton(text="🧰 AI 工具箱", callback_data="toolbox_menu")]
    )
    return main_menu_markup


# ============================================================
# Handlers：进入工具箱 / 各工具入口 / 购买流程
# ============================================================
@router.callback_query(F.data == "toolbox_menu")
async def show_toolbox(query: CallbackQuery):
    await query.answer()
    text = (
        "<b>🧰 AI 工具箱</b>\n"
        "选择你需要的高级功能，按次付费，余额永久有效：\n"
    )
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=ToolboxKeyboards.toolbox_menu())


@router.callback_query(F.data == "tool_chat")
@premium_required("chat")
async def tool_chat(query: CallbackQuery):
    telegram_id = query.from_user.id
    premium_service.consume_credit(telegram_id, "chat")
    await query.answer()
    await query.message.edit_text(
        "💬 <b>AI 对话模式已开启</b>\n直接发消息给我即可开始对话。\n"
        f"（剩余次数：{premium_service.get_balance(telegram_id, 'chat')}）",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回工具箱", callback_data="toolbox_menu")]
        ])
    )
    # TODO: 在这里设置一个 FSM 状态，标记该用户进入"对话模式"，
    # 后续 @router.message(F.text) 根据状态路由到 AI 对话逻辑，而不是当成搜索关键词


@router.callback_query(F.data == "tool_img_gen")
@premium_required("img_gen")
async def tool_img_gen(query: CallbackQuery):
    telegram_id = query.from_user.id
    premium_service.consume_credit(telegram_id, "img_gen")
    await query.answer()
    await query.message.edit_text(
        "🎨 <b>生成图片</b>\n请发送你想生成的图片描述（提示词）。\n"
        f"（剩余次数：{premium_service.get_balance(telegram_id, 'img_gen')}）",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回工具箱", callback_data="toolbox_menu")]
        ])
    )
    # TODO: 设置 FSM 状态等待用户输入提示词，拿到后调用图片生成 API


@router.callback_query(F.data == "tool_video_gen")
@premium_required("video_gen")
async def tool_video_gen(query: CallbackQuery):
    telegram_id = query.from_user.id
    premium_service.consume_credit(telegram_id, "video_gen")
    await query.answer()
    await query.message.edit_text(
        "🎬 <b>生成视频</b>\n请发送视频描述（提示词）。\n"
        f"（剩余次数：{premium_service.get_balance(telegram_id, 'video_gen')}）",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回工具箱", callback_data="toolbox_menu")]
        ])
    )


@router.callback_query(F.data == "tool_img_edit")
@premium_required("img_edit")
async def tool_img_edit(query: CallbackQuery):
    telegram_id = query.from_user.id
    premium_service.consume_credit(telegram_id, "img_edit")
    await query.answer()
    await query.message.edit_text(
        "🖼️ <b>处理图片</b>\n请发送需要处理的图片。\n"
        f"（剩余次数：{premium_service.get_balance(telegram_id, 'img_edit')}）",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回工具箱", callback_data="toolbox_menu")]
        ])
    )


@router.callback_query(F.data == "tool_video_edit")
@premium_required("video_edit")
async def tool_video_edit(query: CallbackQuery):
    telegram_id = query.from_user.id
    premium_service.consume_credit(telegram_id, "video_edit")
    await query.answer()
    await query.message.edit_text(
        "📹 <b>处理视频</b>\n请发送需要处理的视频。\n"
        f"（剩余次数：{premium_service.get_balance(telegram_id, 'video_edit')}）",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回工具箱", callback_data="toolbox_menu")]
        ])
    )


# ============================================================
# 支付流程（Telegram Stars）
# ============================================================
@router.callback_query(F.data.startswith("buy_"))
async def initiate_purchase(query: CallbackQuery):
    """
    callback_data 格式: buy_{tool_key}_{quantity}
    发送一个 Stars 发票，用户在 Telegram 内直接完成支付
    """
    await query.answer()
    _, tool_key, qty_str = query.data.split("_", 2)
    quantity = int(qty_str)
    tool = TOOLS.get(tool_key)
    if not tool:
        await query.message.answer("⚠️ 未知商品")
        return

    unit_price = tool.stars_price
    total_price = unit_price * quantity
    if quantity >= 5:
        total_price = int(total_price * 0.9)  # 5 次及以上 9 折

    await query.message.answer_invoice(
        title=f"{tool.title} x{quantity}",
        description=f"{tool.description}（{quantity} 次使用额度）",
        payload=f"{tool_key}:{quantity}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{tool.title} x{quantity}", amount=total_price)],
    )


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_q: PreCheckoutQuery):
    payload = pre_checkout_q.invoice_payload
    try:
        tool_key, quantity_str = payload.split(":")
        if tool_key not in TOOLS or int(quantity_str) <= 0:
            await pre_checkout_q.answer(ok=False, error_message="商品信息有误，请重新购买")
            return
    except Exception:
        await pre_checkout_q.answer(ok=False, error_message="订单解析失败，请重新购买")
        return

    await pre_checkout_q.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    """支付成功后到账，发放额度"""
    payment = message.successful_payment
    payload = payment.invoice_payload
    tool_key, quantity_str = payload.split(":")
    quantity = int(quantity_str)
    telegram_id = message.from_user.id

    premium_service.add_credit(telegram_id, tool_key, amount=quantity)
    tool = TOOLS[tool_key]

    logger.info(f"用户 {telegram_id} 购买 {tool_key} x{quantity}，花费 {payment.total_amount} Stars")
    await message.answer(
        f"✅ 购买成功！\n"
        f"<b>{tool.emoji} {escape(tool.title)}</b> 已增加 <b>{quantity}</b> 次使用额度\n"
        f"当前剩余：<b>{premium_service.get_balance(telegram_id, tool_key)}</b> 次",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧰 返回工具箱", callback_data="toolbox_menu")]
        ])
    )