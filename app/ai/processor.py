import asyncio
import json

from sqlalchemy import select

from app.ai.output_guard import sanitize_summary_data
from app.ai.summarizer import ProjectSummarizer
from app.collectors.base import BaseAnalyzer
from app.config import settings
from app.database import SessionLocal
from app.database.models import ContentItem, Summary
from app.logger import AppLogger

logger = AppLogger.get(__name__)


class BatchSummary:
    def __init__(self,
                 analyzer: BaseAnalyzer = None,
                 source=None):
        self.analyzer = analyzer
        self.source = source

    def get_unprocessed_projects(self, limit: int | None = None):
        # processed==2（失败）也纳入重新拉取，不再是终态——之前只选
        # processed==0 导致失败条目永远不会被重新处理。目前是无条件重试，
        # 没有次数上限/退避（ADR-0001 §7 提到的"退避重试"仍是缺口，是已知
        # 取舍，不是本次改动范围）：永久性失败的条目会在每轮 process_backlog
        # 里被无限重试，需要时再补 retry_count/退避。
        db = SessionLocal()
        try:
            summarized_ids = select(Summary.project_id)
            query = (
                db.query(ContentItem)
                .filter(ContentItem.processed.in_([0, 2]))
                .filter(~ContentItem.id.in_(summarized_ids))
            )
            if isinstance(self.source, (list, tuple, set)):
                query = query.filter(ContentItem.source.in_(list(self.source)))
            elif self.source:
                query = query.filter(ContentItem.source == self.source)
            # 先做 processed==0 的新条目，再回头重试 processed==2 的旧失败，
            # 否则一批陈年失败条目会把每轮配额吃光，新采集的内容永远排不上。
            query = query.order_by(ContentItem.processed.asc(), ContentItem.id.desc())
            if limit:
                query = query.limit(limit)
            return query.all()
        finally:
            db.close()

    async def process_batch(self, batch_size = 1, limit: int | None = None) -> dict:
        # limit：每轮最多取多少条。不限量时一轮会把整个积压（可能上千条）全部
        # 排进任务队列，单条最坏要等满 LLM 超时，一轮跑几个小时，30 分钟的
        # process_backlog 节奏完全失效。
        limit = limit or settings.batch_process_limit
        projects = self.get_unprocessed_projects(limit=limit)
        if not projects:
            logger.info("📊 没有待处理项目")
            return {"total": 0, "success": 0, "failed": 0, "skipped": 0}
        logger.info(f"🚀 开始处理 {len(projects)} 个项目（上限 {limit}）")

        success = 0
        failed = 0
        skipped = 0
        # provider 全挂时的熔断：第一条就证明所有候选不可用的话，后面几百条
        # 只会重复同样的超时，既拖垮这一轮，又把条目全标成 processed=2 污染
        # 成功率统计。这里一旦探测到就直接跳过剩余条目，等下一轮重试。
        state = {"provider_down": False}
        semaphore = asyncio.Semaphore(batch_size)
        async with ProjectSummarizer() as summarizer:
            tasks = [
                self._process_single(summarizer, project, semaphore, state)
                for project in projects
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, dict) and result.get("success"):
                    success += 1
                elif isinstance(result, dict) and result.get("skipped"):
                    skipped += 1
                else:
                    failed += 1

        if state["provider_down"]:
            logger.error(f"🛑 所有 LLM provider 候选不可用，本轮熔断：跳过 {skipped} 条，等待下一轮")
        logger.info(f"✅ 处理完成: 成功 {success}, 失败 {failed}, 跳过 {skipped}")
        return {
            "total": len(projects),
            "success": success,
            "failed": failed,
            "skipped": skipped,
        }

    async def _process_single(self, summarizer: ProjectSummarizer, project: ContentItem, semaphore, state: dict | None = None) -> dict:
        state = state if state is not None else {"provider_down": False}
        async with semaphore:
            if state.get("provider_down"):
                # 熔断后剩下的条目保持原状态，不落 processed=2：这是基础设施
                # 故障，不是条目本身处理失败。
                return {"success": False, "skipped": True, "project_id": project.id}
            db = SessionLocal()
            try:
                logger.info(f"⏳ 处理: {project.title}")
                prompt = await self.analyzer.build_context(project.url)
                summary_data = await summarizer.summarize(prompt)

                if "error" not in summary_data:
                    summary_data = sanitize_summary_data(summary_data, item_id=project.id)
                    summary = Summary(
                        project_id=project.id,
                        one_liner=summary_data.get("one_liner", ""),
                        summary=summary_data.get("summary", ""),
                        use_cases=summary_data.get("use_cases", ""),
                        pros=json.dumps(summary_data.get("pros", [])),
                        cons=json.dumps(summary_data.get("cons", [])),
                        score=summary_data.get("score", 0),
                        tags=json.dumps(summary_data.get("tags", [])),
                        install_step=summary_data.get("install_step", ""),
                        maturity_level=summary_data.get("maturity_level", ""),
                        source_quotes=summary_data.get("source_quotes", None),  # Q3/Q6 新闻原文摘录
                        model_used=summary_data.get("model", ""),
                        token_cost=summary_data.get("token_cost", 0.0)
                    )
                    db.merge(summary)
                    # project 是另一个（已关闭的）session 查出来的，跟当前 db 不是同一个
                    # session/identity map：直接 `project.processed = 1` 只是改内存属性，
                    # db.commit() 根本看不到这个改动，永远不会真正落库。用显式 UPDATE 绑定
                    # 到当前 db session 上才会真正写进去。
                    self._mark_processed(db, project.id, 1)
                    db.commit()
                    logger.info(f"✅ {project.title}")
                    return {"success": True, "project_id": project.id}
                else:
                    logger.warning(f"⚠️ {project.title}: {summary_data.get('error')}")
                    if summary_data.get("provider_down"):
                        # 所有 provider 候选都不可用：条目本身没问题，保持原
                        # processed 状态等下一轮，同时给本轮打上熔断标记。
                        state["provider_down"] = True
                        return {"success": False, "skipped": True, "project_id": project.id}
                    self._mark_processed(db, project.id, 2)
                    db.commit()
                    return {"success": False, "error": summary_data.get('error')}
            except Exception as e:
                logger.error(f"❌ {project.title}: {str(e)}")
                # 上面任一次 db.commit() 失败都会走到这——这时 session 可能处于
                # aborted 事务状态，不先 rollback 直接再 commit 会抛
                # InFailedSqlTransaction，把"标记失败"这个动作本身也搞失败，
                # 条目就会一直停在 processed=0，被下一轮批处理无限重跑。
                db.rollback()
                try:
                    self._mark_processed(db, project.id, 2)
                    db.commit()
                except Exception as mark_err:
                    logger.error(f"❌ 标记失败状态也失败 {project.title}: {mark_err}")
                    db.rollback()
                return {"success": False, "error": str(e)}
            finally:
                db.close()
                await asyncio.sleep(0.5)

    @staticmethod
    def _mark_processed(db, project_id: int, state: int) -> None:
        db.query(ContentItem).filter(ContentItem.id == project_id).update({"processed": state})


if __name__ == '__main__':
    batch_processor = BatchSummary()
    asyncio.run(batch_processor.process_batch(batch_size=2))