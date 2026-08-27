import asyncio
import signal

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.middlewares import ThrottlingMiddleware, ToolRateLimitMiddleware, EnsureUserMiddleware
from app.config import settings
from app.redis_client import get_redis
from app.bot.core_handlers import BOT_COMMANDS, router as core_router
from app.bot.github_handler import router as github_router
from app.bot.huggingface_handler import router as huggingface_router
from app.bot.toolbox_handler import router as toolbox_router
from app.bot.subscription_handler import router as subscription_router
from app.bot.review_handler import router as review_router
from app.bot.llm_leaderboard_handler import router as leaderboard_router
from app.logger import AppLogger
from app.scheduler.jobs import JobScheduler

logger = AppLogger.get(__name__)

class BotManager:
    def __init__(self):
        self.bot = None
        self.dp = None
        self.scheduler = None
        self._shutdown_event = None

    async def setup(self):
        logger.info("🔧 初始化 Bot 配置...")
        self.bot = Bot(token=settings.telegram_bot_token)
        await self.bot.set_my_commands(BOT_COMMANDS)
        
        self.dp = Dispatcher(storage=RedisStorage(redis=get_redis()))
        self._shutdown_event = asyncio.Event()

        self.dp.include_router(core_router)
        self.dp.include_router(github_router)
        self.dp.include_router(huggingface_router)
        self.dp.include_router(toolbox_router)
        self.dp.include_router(subscription_router)
        self.dp.include_router(review_router)
        self.dp.include_router(leaderboard_router)

        self.dp.update.outer_middleware(EnsureUserMiddleware())
        self.dp.message.middleware(ThrottlingMiddleware(min_interval=0.8))
        self.dp.callback_query.middleware(ThrottlingMiddleware(min_interval=0.5))
        self.dp.callback_query.middleware(
            ToolRateLimitMiddleware(max_calls=5, window_seconds=60)
        )
        logger.info("✅ Bot 配置完成")

    async def start_scheduler(self):
        logger.info("⏰ 启动定时调度器...")

        try:
            self.scheduler = JobScheduler()
            await self.scheduler.setup()
            logger.info("✅ 定时调度器已启动")
        except Exception as e:
            logger.error(f"❌ 启动定时调度器失败: {e}")
            raise

    async def stop_scheduler(self):
        if self.scheduler is None:
            return

        logger.info("⏰ 停止定时调度器...")
        try:
            self.scheduler.stop()
            logger.info("✅ 定时调度器已停止")
        except Exception as e:
            logger.warning(f"⚠️ 停止定时调度器时出错: {e}")

    async def shutdown(self):
        logger.info("⏹️ Bot 正在关闭...")

        await self.stop_scheduler()
        if self.dp and self.dp.storage:
            try:
                await self.dp.storage.close()
                logger.debug("✅ 存储已关闭")
            except Exception as e:
                logger.warning(f"⚠️ 关闭存储时出错: {e}")

        if self.bot:
            try:
                await self.bot.session.close()
                logger.debug("✅ Bot 会话已关闭")
            except Exception as e:
                logger.warning(f"⚠️ 关闭 Bot 会话时出错: {e}")

        if self._shutdown_event:
            self._shutdown_event.set()
        logger.info("✅ Bot 已安全关闭")

    def _setup_signal_handlers(self, loop: asyncio.AbstractEventLoop):
        logger.info("📡 设置信号处理器...")

        def signal_handler(signum: int):
            logger.info(f"📡 收到信号: {signal.Signals(signum).name}")
            asyncio.create_task(self.shutdown())

        # 处理 SIGINT (Ctrl+C) 和 SIGTERM (kill)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler, sig)
                logger.debug(f"✅ 已注册 {sig.name} 信号处理器")
            except NotImplementedError:
                logger.debug(f"⚠️ 平台不支持 {sig.name} 信号处理")

    async def run(self):
        logger.info("🚀 Bot 启动流程开始...")
        try:
            await self.setup()
            await self.start_scheduler()

            loop = asyncio.get_event_loop()
            self._setup_signal_handlers(loop)

            logger.info("📡 启动 Telegram Polling...")
            await self.bot.delete_webhook(drop_pending_updates=True)

            # 5. 创建 polling 任务
            polling_task = asyncio.create_task(
                self.dp.start_polling(
                    self.bot,
                    allowed_updates=self.dp.resolve_used_update_types()
                )
            )

            logger.info("✅ Bot 已启动，等待消息...")
            await self._shutdown_event.wait()

            logger.info("📡 收到关闭信号，取消 polling...")
            polling_task.cancel()
            try:
                await asyncio.wait_for(polling_task, timeout=5)
            except asyncio.CancelledError:
                logger.debug("✅ Polling 任务已取消")
            except asyncio.TimeoutError:
                logger.warning("⚠️ Polling 任务超时")

        except Exception as e:
            logger.error(f"❌ Bot 运行出错: {e}", exc_info=True)
            raise

        finally:
            logger.info("🧹 清理资源...")
            await self.shutdown()
            logger.info("🏁 Bot 已完全关闭")


async def main():
    manager = BotManager()
    await manager.run()


if __name__ == "__main__":
    asyncio.run(main())