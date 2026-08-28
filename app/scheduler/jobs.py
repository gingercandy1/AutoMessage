import asyncio
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.ai.processor import BatchSummary
from app.collectors.base import BaseCollector, BaseAnalyzer
from app.collectors.news.news_base import NEWS_SOURCES
from app.config import settings
from app.database import SessionLocal
from app.database.models import Subscription
from app.health import record_pipeline_run
from app.logger import AppLogger
from app.services.publisher import DailyPublisher
from app.services.review_service import ReviewService

from app.collectors.github_collector import GitHubTrendingCollector
from app.collectors.github_collector import GitHubCollector
from app.collectors.github_analyzer import GitHubAnalyzer
from app.collectors.huggingface_collector import HuggingFaceCollector
from app.collectors.huggingface_analyzer import HuggingFaceAnalyzer
from app.collectors.news.news_collector import NewsCollector
from app.collectors.news.news_analyzer import NewsAnalyzer
from app.collectors.news.producthunt_collector import ProductHuntCollector
from app.collectors.news.official_blog_collector import OfficialBlogCollector
from app.collectors.hf_papers_collector import HFPapersCollector
from app.collectors.llm_leaderboard_collector import LLMLeaderboardCollector

logger = AppLogger.get(__name__)


class JobScheduler:
    """任务调度器"""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.collector_dict = BaseCollector.collect_dict()
        self.analyzer_dict = BaseAnalyzer.analyzer_dict()
        self.batch_processor_dict = self._build_batch_processors()

    def _build_batch_processors(self) -> dict:
        """批量摘要处理器。新闻族（source ∈ NEWS_SOURCES）统一由 NewsAnalyzer
        覆盖，避免每个新闻源单独一个 BatchSummary 造成重复摘要或缺失 analyzer。"""
        processors: dict = {}
        for key in self.collector_dict.keys():
            # 新闻族统一走 NewsAnalyzer；llm_leaderboard 是结构化榜单数据本身就是价值，
            # 不需要 LLM 生成一句话总结（此前分析了但没地方显示，纯浪费 token），
            # 采集时直接 processed=1（见 llm_leaderboard_collector.py）。
            if key in NEWS_SOURCES or key == "llm_leaderboard":
                continue
            processors[key] = BatchSummary(
                analyzer=self.analyzer_dict.get(key), source=key
            )
        news_analyzer = self.analyzer_dict.get("news")
        processors["news"] = BatchSummary(
            analyzer=news_analyzer, source=list(NEWS_SOURCES)
        )
        return processors

    async def _watchdog_tick(self):
        """看门狗定时自检：按 watchdog_interval_minutes 周期运行，异常则走告警通道。"""
        if not settings.watchdog_enabled:
            logger.debug("看门狗已禁用，跳过自检")
            return
        try:
            from app.watchdog.monitor import WatchdogMonitor
            monitor = WatchdogMonitor()
            await monitor.run_and_alert()
        except Exception as e:
            logger.error(f"看门狗自检异常: {e}", exc_info=True)

    async def _track(self, job_id: str, coro):
        """包裹任意任务，记录心跳并在失败时可追溯。"""
        try:
            from app.watchdog.checks import record_job_heartbeat
            result = await coro
            record_job_heartbeat(job_id, "ok")
            return result
        except Exception as e:
            from app.watchdog.checks import record_job_heartbeat
            record_job_heartbeat(job_id, "failed", str(e)[:300])
            raise

    # --- 看门狗包裹的调度入口（供 APScheduler 直接调用，避免 lambda 闭包问题） ---
    async def _daily_collect_tracked(self):
        await self._track("daily_collect", self.daily_collect())
    async def _process_backlog_tracked(self):
        await self._track("process_backlog", self.daily_process())
    async def _news_collect_tracked(self):
        await self._track("news_collect_afternoon", self.news_collect())
    async def _daily_push_tracked(self):
        await self._track("daily_push", self.daily_push())
    async def _lightweight_snapshot_tracked(self):
        await self._track("lightweight_snapshot", self.lightweight_snapshot())
    async def _weekly_leaderboard_tracked(self):
        await self._track("weekly_leaderboard", self.weekly_leaderboard())

    async def setup(self):
        # 每天 8:00 采集项目
        self.scheduler.add_job(
            self._daily_collect_tracked,
            CronTrigger(hour=8, minute=0),
            id="daily_collect",
            name="每日采集项目"
        )
        # 批处理（摘要）每 30 分钟检查一次积压，不再绑死在 08:00 采集后一次性跑完；
        # 新闻优先处理，处理完立即送审（daily_process 内部实现），不用等其它源。
        self.scheduler.add_job(
            self._process_backlog_tracked,
            IntervalTrigger(minutes=30),
            id="process_backlog",
            name="批处理积压(30min)"
        )
        # T06 每天 13:00 第二次采集新闻类源（时效性）
        self.scheduler.add_job(
            self._news_collect_tracked,
            CronTrigger(hour=13, minute=0),
            id="news_collect_afternoon",
            name="午后新闻采集"
        )
        # 每天 9:00 主动推送（ADR-0003 bot push）
        self.scheduler.add_job(
            self._daily_push_tracked,
            CronTrigger(hour=9, minute=0),
            id="daily_push",
            name="每日主动推送"
        )
        # 送审（Q4 审查闸门）不再单独固定 10:00 跑一次：daily_process 里新闻批
        # 处理完立即调用 daily_review()，跟着 30 分钟积压检查节奏走，见 process_backlog。
        # Q9 每 6h 轻量快照：14:00 / 20:00 重新采集 GitHub + HF 指标（不触发 LLM）
        self.scheduler.add_job(
            self._lightweight_snapshot_tracked,
            CronTrigger(hour="14,20", minute=0),
            id="lightweight_snapshot",
            name="轻量快照(6h)"
        )
        # #07 每周 LLM 排行榜采集（周一 07:30）
        self.scheduler.add_job(
            self._weekly_leaderboard_tracked,
            CronTrigger(day_of_week="mon", hour=7, minute=30),
            id="weekly_leaderboard",
            name="每周LLM排行榜"
        )
        # 看门狗：每 watchdog_interval_minutes 分钟自检（可追溯 + 告警通道）
        if settings.watchdog_enabled:
            self.scheduler.add_job(
                self._watchdog_tick,
                IntervalTrigger(minutes=settings.watchdog_interval_minutes),
                id="watchdog",
                name="看门狗自检",
                next_run_time=datetime.now(timezone.utc),
            )
            logger.info(f"🐶 看门狗已启用，间隔 {settings.watchdog_interval_minutes}min，冷却 {settings.watchdog_cooldown_minutes}min")

        self.scheduler.start()
        logger.info("✅ 定时调度器已启动")

    async def daily_run(self):
        await self.daily_collect()
        await self.daily_process()

    async def daily_collect(self):
        logger.info("🚀 启动每日采集流程...")
        run_started = datetime.now(timezone.utc)
        collect_failures = 0
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
                            collect_failures += 1
                            logger.error(f"❌ 采集失败 [{source}]: {e}")
                        await asyncio.sleep(2)
                    logger.info(f"✅ {source}采集完成: {total} 个新项目")
                record_pipeline_run(
                    run_type="daily", source=source, collected=total,
                    failed_by_stage={"collect": collect_failures},
                    started_at=run_started,
                )
        except Exception as e:
            logger.error(f"❌ 采集调度失败: {e}")

    async def news_collect(self):
        """T06：每日第二次采集，只跑新闻类源（8:00 之外的 13:00 场）。
        新闻源 = collector NAME ∈ NEWS_SOURCES。非新闻源（GitHub、HF 模型、
        LLM 排行榜）不受影响。两次运行共用跨源去重（NewsCluster 合并）。
        """
        logger.info("📰 启动午后新闻采集（第二次）...")
        run_started = datetime.now(timezone.utc)
        collect_failures = 0
        try:
            for source, collectors in self.collector_dict.items():
                if source not in NEWS_SOURCES:
                    continue
                total = 0
                for collector in collectors:
                    configs = collector.configs()
                    for config in configs:
                        try:
                            result = await collector.fetch(config)
                            count = len(result) if isinstance(result, (list, tuple)) else (result or 0)
                            total += count
                        except Exception as e:
                            collect_failures += 1
                            logger.error(f"❌ 新闻采集失败 [{source}]: {e}")
                        await asyncio.sleep(2)
                    logger.info(f"✅ {source}新闻采集完成: {total} 条")
                record_pipeline_run(
                    run_type="news", source=source, collected=total,
                    failed_by_stage={"collect": collect_failures},
                    started_at=run_started,
                )
        except Exception as e:
            logger.error(f"❌ 新闻采集调度失败: {e}")

    async def _run_processor(self, key: str) -> dict:
        try:
            result = await self.batch_processor_dict[key].process_batch(batch_size=2)
            logger.info(
                f"✅ 批处理完成 [{key}]: 成功 {result['success']}, 失败 {result['failed']}"
            )
            return result
        except Exception as e:
            logger.error(f"❌ 批处理失败 [{key}]: {e}")
            return {"success": 0, "failed": 1}

    async def daily_process(self):
        """批处理积压（每 30 分钟一次，见 process_backlog job）。新闻优先：
        新闻批处理完立即调 daily_review() 送审，不等其它源处理完——用户需求：
        处理和送审要紧接着，不能靠"猜 10:00 前处理完没"。"""
        logger.info("🔁 启动批处理流程（新闻优先）...")
        run_started = datetime.now(timezone.utc)
        ok_total = 0
        fail_total = 0
        try:
            news_result = await self._run_processor("news")
            ok_total += news_result["success"]
            fail_total += news_result["failed"]
            if news_result["success"]:
                await self.daily_review()

            for key in self.batch_processor_dict:
                if key == "news":
                    continue
                result = await self._run_processor(key)
                ok_total += result["success"]
                fail_total += result["failed"]

            record_pipeline_run(
                run_type="daily", source="process", collected=0,
                processed_ok=ok_total, processed_failed=fail_total,
                failed_by_stage={"process": fail_total},
                started_at=run_started,
            )
        except Exception as e:
            logger.error(f"❌ 批处理调度失败: {e}")

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
        run_started = datetime.now(timezone.utc)
        logger.info("📸 启动轻量快照（GitHub + HF）...")
        targets = ["github", "huggingface"]
        snap_collected = 0
        snap_failures = 0
        for source in targets:
            collectors = self.collector_dict.get(source, [])
            for collector in collectors:
                for config in collector.configs():
                    try:
                        count = await collector.fetch(config)
                        snap_collected += count if isinstance(count, int) else 0
                    except Exception as e:
                        snap_failures += 1
                        logger.error(f"❌ 轻量快照失败 [{source}]: {e}")
                    await asyncio.sleep(1)
        record_pipeline_run(
            run_type="lightweight", source="github+huggingface",
            collected=snap_collected,
            failed_by_stage={"collect": snap_failures},
            started_at=run_started,
        )
        logger.info("✅ 轻量快照完成")

    async def weekly_leaderboard(self):
        """#07 每周采集 LLM 排行榜"""
        logger.info("🤖 启动每周 LLM 排行榜采集...")
        try:
            collector = LLMLeaderboardCollector()
            items = await collector.fetch()
            logger.info(f"✅ LLM 排行榜采集完成: {len(items)} 条")
        except Exception as e:
            logger.error(f"❌ LLM 排行榜采集失败: {e}")

    async def daily_push(self):
        """定时主动推送：推给所有已订阅用户/群组。"""
        logger.info("📤 启动主动推送流程...")
        db = SessionLocal()
        try:
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

    def stop(self):
        self.scheduler.shutdown()
        logger.info("✅ 定时调度已停止")


async def start_scheduler():
    job_scheduler = JobScheduler()
    await job_scheduler.daily_run()


if __name__ == '__main__':
    asyncio.run(start_scheduler())
