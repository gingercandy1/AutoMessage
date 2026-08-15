from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.collectors.llm_leaderboard_collector import (
    PLATFORM_AA,
    PLATFORM_LMARENA,
    PLATFORM_OPENROUTER,
    PLATFORM_VELLUM,
    PLATFORM_SEAL,
)
from app.database import SessionLocal
from app.database.models import ContentItem
from app.logger import AppLogger

logger = AppLogger.get(__name__)
router = Router()

PAGE_SIZE = 10

PLATFORM_LABELS = {
    PLATFORM_AA: "Artificial Analysis",
    PLATFORM_LMARENA: "LMArena",
    PLATFORM_OPENROUTER: "OpenRouter",
    PLATFORM_VELLUM: "Vellum",
    PLATFORM_SEAL: "SEAL",
}


def _render_rows(rows) -> str:
    lines = []
    for it in rows:
        m = it.metrics or {}
        raw = it.raw_data or {}
        rank = m.get("rank", "?")
        score = m.get("score", "?")
        platform = PLATFORM_LABELS.get(m.get("platform"), m.get("platform", ""))
        links = []
        if raw.get("provider_url"):
            links.append(f"[供应商]({raw['provider_url']})")
        if raw.get("hf_id"):
            links.append(f"[HF](https://huggingface.co/{raw['hf_id']})")
        link_txt = " · ".join(links)
        lines.append(f"{rank}. <b>{escape(it.title)}</b> · {score} · {platform}  {link_txt}")
    return "\n".join(lines) if lines else "（暂无数据）"


def escape(s) -> str:
    from html import escape as _e
    return _e(str(s))


@router.message(Command("llm_start"))
async def llm_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 排行榜 Top10", callback_data="llm:top:1")],
        [InlineKeyboardButton(text="🤗 只看 HuggingFace 模型", callback_data="llm:hf:1")],
        [InlineKeyboardButton(text=f"🔬 {PLATFORM_LABELS[PLATFORM_AA]}",
                              callback_data=f"llm:plat:{PLATFORM_AA}:1"),
         InlineKeyboardButton(text=f"🏟️ {PLATFORM_LABELS[PLATFORM_LMARENA]}",
                              callback_data=f"llm:plat:{PLATFORM_LMARENA}:1")],
        [InlineKeyboardButton(text=f"🌐 {PLATFORM_LABELS[PLATFORM_OPENROUTER]}",
                              callback_data=f"llm:plat:{PLATFORM_OPENROUTER}:1"),
         InlineKeyboardButton(text=f"⚙️ {PLATFORM_LABELS[PLATFORM_VELLUM]}",
                              callback_data=f"llm:plat:{PLATFORM_VELLUM}:1")],
        [InlineKeyboardButton(text=f"🛡️ {PLATFORM_LABELS[PLATFORM_SEAL]}",
                              callback_data=f"llm:plat:{PLATFORM_SEAL}:1")],
    ])
    await message.answer("🤖 <b>LLM 排行榜</b>\n选择查看方式：", reply_markup=kb, parse_mode="HTML")


@router.message(Command("llm_top"))
async def llm_top(message: Message):
    page = 1
    if message.text and " " in message.text:
        try:
            page = max(1, int(message.text.split(" ", 1)[1].strip()))
        except ValueError:
            pass
    await _send_page(message, page, only_hf=False)


@router.callback_query(F.data.startswith("llm:top:"))
async def llm_top_cb(query):
    page = int(query.data.split(":")[-1])
    await _send_page(query.message, page, only_hf=False, edit=True)
    await query.answer()


@router.callback_query(F.data.startswith("llm:hf:"))
async def llm_hf_cb(query):
    page = int(query.data.split(":")[-1])
    await _send_page(query.message, page, only_hf=True, edit=True)
    await query.answer()


@router.callback_query(F.data.startswith("llm:plat:"))
async def llm_plat_cb(query):
    _, _, platform, page = query.data.split(":")
    await _send_page(query.message, int(page), platform=platform, edit=True)
    await query.answer()


def _query_page(page: int, only_hf: bool = False, platform: str = None):
    db = SessionLocal()
    try:
        q = db.query(ContentItem).filter(ContentItem.source == "llm_leaderboard")
        if only_hf:
            q = q.filter(ContentItem.raw_data["hf_id"].isnot(None))
        if platform:
            q = q.filter(ContentItem.metrics["platform"].astext == platform)
        total = q.count()
        rows = (
            q.order_by(ContentItem.metrics["rank"].astext.cast(db.Integer))
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
        return total, rows
    finally:
        db.close()


async def _send_page(target, page: int, only_hf: bool = False, platform: str = None, edit: bool = False):
    total, rows = _query_page(page, only_hf, platform)
    if platform:
        title = f"🏆 LLM 排行榜 · {PLATFORM_LABELS.get(platform, platform)}"
        prefix = f"plat:{platform}"
    elif only_hf:
        title = "🏆 LLM 排行榜（仅 HuggingFace）"
        prefix = "hf"
    else:
        title = "🏆 LLM 排行榜"
        prefix = "top"
    text = f"{title}\n（第 {page} 页 / 共 {max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)} 页）\n\n"
    text += _render_rows(rows)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"llm:{prefix}:{max(1, page - 1)}"),
            InlineKeyboardButton(text="➡️ 下一页", callback_data=f"llm:{prefix}:{page + 1}"),
        ]
    ])
    if edit:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
