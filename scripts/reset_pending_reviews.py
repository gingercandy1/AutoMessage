"""重置审查状态管理脚本。

用途：把之前推送到审查频道但处于 pending 的条目撤销（review_status 置回
NULL），使其重新进入待审队列，下轮送审会重新推送。

用法：
  python scripts/reset_pending_reviews.py          # 仅预览，不修改数据
  python scripts/reset_pending_reviews.py --reset  # 执行重置（pending -> 待审）
  python scripts/reset_pending_reviews.py --reset --push   # 重置后立即送审一批

说明：
- 只影响 review_status = 'pending' 的条目；approved / rejected 不动。
- 审查频道里的旧卡片无法由 bot 追溯删除（Bot API 不能枚举历史消息），
  需要手动清理一次；之后的卡片会被 ✅/❏ 自动销毁。
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func

from app.database import SessionLocal
from app.database.models import ContentItem


def count_by_status() -> dict:
    db = SessionLocal()
    try:
        rows = db.query(ContentItem.review_status, func.count()).group_by(
            ContentItem.review_status
        ).all()
        return {rs: c for rs, c in rows}
    finally:
        db.close()


def reset_pending() -> int:
    db = SessionLocal()
    try:
        n = db.query(ContentItem).filter(
            ContentItem.review_status == "pending"
        ).update({"review_status": None}, synchronize_session=False)
        db.commit()
        return n
    finally:
        db.close()


async def push_now() -> None:
    from aiogram import Bot

    from app.config import settings
    from app.services.review_service import ReviewService

    bot = Bot(token=settings.telegram_bot_token)
    try:
        sent = await ReviewService().send_for_review(bot)
        print(f"✅ 本轮已重新送审 {sent} 条")
    finally:
        await bot.session.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="执行重置（pending -> 待审）")
    parser.add_argument("--push", action="store_true", help="重置后立即送审一批（需配合 --reset）")
    args = parser.parse_args()

    before = count_by_status()
    print(f"当前状态分布: {before}")

    if not args.reset:
        print("（预览模式，加 --reset 执行）")
        return

    n = reset_pending()
    print(f"✅ 已把 {n} 条 pending 重置为待审")
    after = count_by_status()
    print(f"重置后分布: {after}")

    if args.push:
        await push_now()


if __name__ == "__main__":
    asyncio.run(main())
