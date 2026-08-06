import asyncio
import json

from sqlalchemy import select

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
                 source: str = "gitHub",
                 provider: str = None):
        self.provider = provider or settings.default_llm_provider
        self.analyzer = analyzer
        self.source = source

    def get_unprocessed_projects(self):
        db = SessionLocal()
        try:
            summarized_ids = select(Summary.project_id)
            return (
                db.query(ContentItem)
                .filter((ContentItem.processed == 0) & (ContentItem.source == self.source))
                .filter(~ContentItem.id.in_(summarized_ids))
                .all()
            )
        finally:
            db.close()

    async def process_batch(self, batch_size = 1) -> dict:
        projects = self.get_unprocessed_projects()
        if not projects:
            logger.info("📊 没有待处理项目")
            return {"total": 0, "success": 0, "failed": 0}
        logger.info(f"🚀 开始处理 {len(projects)} 个项目 (provider: {self.provider})")

        success = 0
        failed = 0
        semaphore = asyncio.Semaphore(batch_size)
        async with ProjectSummarizer(provider=self.provider) as summarizer:
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
                        model_used=summary_data.get("model", self.provider),
                        token_cost=summary_data.get("token_cost", 0.0)
                    )
                    db.merge(summary)
                    project.processed = 1
                    db.commit()
                    logger.info(f"✅ {project.title}")
                    return {"success": True, "project_id": project.id}
                else:
                    logger.warning(f"⚠️ {project.title}: {summary_data.get('error')}")
                    project.processed = 2
                    db.commit()
                    return {"success": False, "error": summary_data.get('error')}
            except Exception as e:
                logger.error(f"❌ {project.title}: {str(e)}")
                project.processed = 2
                db.commit()
                return {"success": False, "error": str(e)}
            finally:
                db.close()
                await asyncio.sleep(0.5)


if __name__ == '__main__':
    batch_processor = BatchSummary()
    asyncio.run(batch_processor.process_batch(batch_size=2))