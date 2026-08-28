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

    def get_unprocessed_projects(self):
        db = SessionLocal()
        try:
            summarized_ids = select(Summary.project_id)
            query = (
                db.query(ContentItem)
                .filter((ContentItem.processed == 0))
                .filter(~ContentItem.id.in_(summarized_ids))
            )
            if isinstance(self.source, (list, tuple, set)):
                query = query.filter(ContentItem.source.in_(list(self.source)))
            elif self.source:
                query = query.filter(ContentItem.source == self.source)
            return query.all()
        finally:
            db.close()

    async def process_batch(self, batch_size = 1) -> dict:
        projects = self.get_unprocessed_projects()
        if not projects:
            logger.info("📊 没有待处理项目")
            return {"total": 0, "success": 0, "failed": 0}
        logger.info(f"🚀 开始处理 {len(projects)} 个项目")

        success = 0
        failed = 0
        semaphore = asyncio.Semaphore(batch_size)
        async with ProjectSummarizer() as summarizer:
            tasks = [
                self._process_single(summarizer, project, semaphore)
                for project in projects
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, dict) and result.get("success"):
                    success += 1
                else:
                    failed += 1

        logger.info(f"✅ 处理完成: 成功 {success}, 失败 {failed}")
        return {
            "total": len(projects),
            "success": success,
            "failed": failed
        }

    async def _process_single(self, summarizer: ProjectSummarizer, project: ContentItem, semaphore) -> dict:
        async with semaphore:
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