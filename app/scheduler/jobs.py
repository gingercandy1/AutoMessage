import asyncio

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.ai.processor import BatchSummary
from app.collectors.base import BaseCollector, BaseAnalyzer
from app.config import settings
from app.database import SessionLocal
from app.database.models import Subscription
from app.logger import AppLogger
from app.services.publisher import DailyPublisher
from app.services.review_service import ReviewService

from app.collectors.github_collector import GitHubCollector, GitHubTrendingCollector
from app.collectors.github_analyzer import GitHubAnalyzer
from app.collectors.huggingface_collector import HuggingFaceCollector
from app.collectors.huggingface_analyzer import HuggingFaceAnalyzer
from app.collectors.news_collector import NewsCollector
from app.collectors.news_analyzer import NewsAnalyzer

logger = AppLogger.get(__name__)


class JobScheduler:
    """任务调度器"""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.collector_dict = BaseCollector.collect_dict()
        self.analyzer_dict = BaseAnalyzer.analyzer_dict()
        self.batch_processor_dict = {
            key: BatchSummary(analyzer=self.analyzer_dict.get(key), source=key)
            for key in self.collector_dict.keys()
        }

    async def setup(self):
        # 每天 8:00 采集项目
        self.scheduler.add_job(
            self.daily_run,
            CronTrigger(hour=8, minute=0),
            id="daily_collect",
            name="每日采集项目"
        )
        # 每天 9:00 主动推送（ADR-0003 bot push）
        self.scheduler.add_job(
            self.daily_push,
            CronTrigger(hour=9, minute=0),
            id="daily_push",
            name="每日主动推送"
        )
        # 每天 10:00 把已摘要新闻送审查群组（Q4 审查闸门）
        self.scheduler.add_job(
            self.daily_review,
            CronTrigger(hour=10, minute=0),
            id="daily_review",
            name="每日送审"
        )
        # Q9 每 6h 轻量快照：14:00 / 20:00 重新采集 GitHub + HF 指标（不触发 LLM）
        self.scheduler.add_job(
            self.lightweight_snapshot,
            CronTrigger(hour="14,20", minute=0),
            id="lightweight_snapshot",
            name="轻量快照(6h)"
        )

        self.scheduler.start()
        logger.info("✅ 定时调度器已启动")

    async def daily_run(self):
        await self.daily_collect()
        await self.daily_process()

    async def daily_collect(self):
        logger.info("🚀 启动每日采集流程...")
        try:
            for source, collectors in self.collector_dict.items():
                total = 0
                for collector in collectors:
                    configs = collector.configs()
                    for config in configs:
                        # Per-(source, collector, config) isolation: one failure must
                        # not abort the whole batch (ADR-0001 §7 failure isolation).
                        try:
                            count = await collector.fetch(config)
                            total += count
                        except Exception as e:
                            logger.error(f"❌ 采集失败 [{source}]: {e}")
                        await asyncio.sleep(2)
                    logger.info(f"✅ {source}采集完成: {total} 个新项目")
        except Exception as e:
            logger.error(f"❌ 采集调度失败: {e}")

    async def daily_process(self):
        logger.info("🌙 启动晚间批处理流程...")
        try:
            for key, batch_processor in self.batch_processor_dict.items():
                try:
                    result = await batch_processor.process_batch(batch_size=2)
                    logger.info(
                        f"✅ 晚间处理完成 [{key}]: "
                        f"成功 {result['success']}, 失败 {result['failed']}"
                    )
                except Exception as e:
                    logger.error(f"❌ 批处理失败 [{key}]: {e}")
        except Exception as e:
            logger.error(f"❌ 晚间处理调度失败: {e}")

    async def daily_review(self):
        """Q4 审查闸门：把 AI 已摘要(processed=1)的新闻送审查群组等待人工 ✅/❌。"""
        logger.info("🔍 启动送审流程...")
        try:
            bot = Bot(token=settings.telegram_bot_token)
            review = ReviewService()
            sent = await review.send_for_review(bot)
            logger.info(f"✅ 送审完成: {sent} 条")
            await bot.session.close()
        except Exception as e:
            logger.error(f"❌ 送审失败: {e}")

    async def lightweight_snapshot(self):
        """Q9 轻量快照：每 6h 重新采集 GitHub + HF 指标并写入 MetricSnapshot（不触发 LLM）。"""
        logger.info("📸 启动轻量快照（GitHub + HF）...")
        targets = ["github", "huggingface"]
        for source in targets:
            collectors = self.collector_dict.get(source, [])
            for collector in collectors:
                for config in collector.configs():
                    try:
                        await collector.fetch(config)
                    except Exception as e:
                        logger.error(f"❌ 轻量快照失败 [{source}]: {e}")
                    await asyncio.sleep(1)
        logger.info("✅ 轻量快照完成")

    async def daily_push(self):
        """定时主动推送（ADR-0003 bot push）：推给所有已订阅用户/群组。"""
        logger.info("📤 启动主动推送流程...")
        db = SessionLocal()
        try:
            # 去重取所有活跃订阅的 telegram_id
            rows = (
                db.query(Subscription.telegram_id)
                .filter(Subscription.is_active == 1)
                .distinct()
                .all()
            )
            subscribers = [r[0] for r in rows]
        finally:
            db.close()

        if not subscribers:
            logger.info("📭 无订阅用户，跳过推送")
            return

        publisher = DailyPublisher()
        try:
            for tg_id in subscribers:
                try:
                    await publisher.publish_to_user(tg_id)
                    logger.info(f"✅ 已推送给 {tg_id}")
                except Exception as e:
                    logger.error(f"❌ 推送给 {tg_id} 失败: {e}")
                await asyncio.sleep(0.5)
        finally:
            await publisher.close()
        await self.setup()
        logger.info("⏰ 定时调度已启动，等待执行...")
        await asyncio.Event().wait()

    def stop(self):
        self.scheduler.shutdown()
        logger.info("✅ 定时调度已停止")


async def start_scheduler():
    job_scheduler = JobScheduler()
    await job_scheduler.daily_run()


if __name__ == '__main__':
    asyncio.run(start_scheduler())
