"""频道评论功能验证脚本。

用法：
  1. 在 Telegram 客户端完成一次性配置：公开频道 -> 管理频道 -> 讨论群组 ->
     绑定一个普通群组（此步只能由频道管理员在客户端操作，Bot API 无此接口）；
  2. 确认 .env 里 public_channel_id 已配置、bot 是频道管理员；
  3. 运行：python scripts/test_channel_post.py
  4. 到频道里查看这条测试帖下方是否出现「💬 评论」按钮；
     出现即配置成功，测试帖可手动删除。

注意：绑定只对新帖子生效，历史帖子不会补上评论按钮。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot

from app.config import settings


async def main() -> None:
    if not settings.telegram_bot_token:
        print("❌ 未配置 telegram_bot_token")
        return
    if not settings.public_channel_id:
        print("❌ 未配置 public_channel_id（公开频道 Chat ID，通常为 @频道名 或 -100 开头数字）")
        return

    bot = Bot(token=settings.telegram_bot_token)
    try:
        await bot.send_message(
            settings.public_channel_id,
            "🧪 评论功能测试帖\n\n"
            "如果你能看到本条消息下方的「💬 评论」按钮并可以点进讨论区发言，"
            "说明频道已成功绑定讨论群。确认后可删除本条消息。",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        print(f"✅ 测试帖已发送到 {settings.public_channel_id}")
        print("👉 请到频道检查该帖是否出现「💬 评论」按钮；确认后删除即可。")
    except Exception as e:
        print(f"❌ 发送失败：{e}")
        print("排查：bot 是否为频道管理员？public_channel_id 是否正确？")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
