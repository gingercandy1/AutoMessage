import asyncio
from dataclasses import dataclass
from datetime import timezone, datetime

from sqlalchemy.dialects.postgresql import insert

from app.api.huggingface_client import HuggingFaceClient
from app.collectors.base import BaseCollector
from app.config import settings
from app.database import SessionLocal
from app.database.models import ContentItem, MetricSnapshot


@dataclass
class HuggingFaceConfig:
    category_key: str = ""
    limit: int = 100
    sort: str = "trendingScore"   # trendingScore | lastModified | downloads | likes
    search_type: str = "hf_hot"   # hf_hot (popular) | hf_trend (recently updated)

class HuggingFaceCollector(BaseCollector):
    NAME = "huggingface"

    def __init__(self, client=None):
        self.client = client if client else HuggingFaceClient(settings.hugging_face_token)

    def _parse_tags(self, tags: list[str]) -> dict:
        """
        解析 HuggingFace 的 tags 列表。
        例如: ["license:mit", "transformers", "text-generation"] 
        -> {"license": "mit", "tags": ["transformers", "text-generation"]}
        """
        parsed = {"tags": []}
        if not tags:
            return parsed
        for tag in tags:
            if ":" in tag:
                key, value = tag.split(":", 1)
                parsed[key] = value
            else:
                parsed["tags"].append(tag)
        return parsed

    async def fetch(self, config: HuggingFaceConfig) -> int:
        category_key = config.category_key
        limit = config.limit

        pipeline_tag = self.client.PIPELINE_MAP.get(category_key)
        if not pipeline_tag:
            raise ValueError(f"不支援的類別: {category_key}")

        models = await self.client.fetch_by_category(category_key, limit, sort=config.sort)
        if not models: return 0

        records = []
        for i, m in enumerate(models, 1):
            # 过滤无内容可总结的条目：没有 model card 描述、又没有点赞/下载量的
            # 仓库（个人测试仓库、量化 dump、bittensor 镜像之类）——LLM 面对空
            # description 只会每次都失败，摘要阶段会把它们永久标记失败且从不
            # 重试；与其让这类条目堆积在 processed=2 里空耗批次，不如采集时
            # 就不收。hf_trend（按 lastModified 排序）是这类噪音的主要来源。
            description = (m.get("description") or "").strip()
            likes = m.get("likes", 0) or 0
            downloads = m.get("downloads", 0) or 0
            if not description and likes == 0 and downloads == 0:
                continue

            # 解析 tags
            parsed_tags = self._parse_tags(m.get("tags", []))

            record = {
                "model_id": m["id"],
                "category": category_key,
                "pipeline_tag": pipeline_tag,
                "likes": m.get("likes", 0),
                "downloads": m.get("downloads", 0),
                "rank": i,
                "created_at": m.get("createdAt"),
                "library_name": m.get("library_name"),
                "trending_score": m.get("trendingScore"),
                "last_modified": m.get("lastModified"),
                "description": m.get("description", ""),
                "author": m["id"].split("/")[0] if "/" in m["id"] else "",
                "private": m["private"],
                "search_type": config.search_type,
            }
            record.update(parsed_tags)
            records.append(record)

        count = await self.save(records)
        return count

    async def save(self, records: list[dict]) -> int:
        db = SessionLocal()
        added = 0
        now = datetime.now(timezone.utc)
        today = now.date()

        try:
            for r in records:
                metrics_data = {
                    "category": r["category"],
                    "pipeline_tag": r["pipeline_tag"],
                    "likes": r["likes"],
                    "downloads": r["downloads"],
                    "rank": r["rank"],
                    "trending_score": r["trending_score"],
                    "last_modified": r.get("last_modified"),
                    "library_name": r["library_name"],
                    "model_created_at": r["created_at"],
                    "search_type": r.get("search_type", "hf_hot"),
                    "license": r.get("license"),
                    "tags": r.get("tags", []),
                    "private": r["private"]
                }
                existing = db.query(ContentItem).filter_by(
                    source=self.NAME,
                    external_id=r["model_id"]
                ).first()
                if existing:
                    existing.title = r["model_id"]
                    existing.url = f"https://huggingface.co/{r['model_id']}"
                    existing.description = r.get("description")
                    existing.author = r.get("author")
                    existing.metrics = metrics_data
                    existing.raw_data = r
                    content_item = existing
                else:
                    content_item = ContentItem(
                        source=self.NAME,
                        external_id=r["model_id"],
                        title=r["model_id"],
                        url=f"https://huggingface.co/{r['model_id']}",
                        description=r.get("description"),
                        author=r.get("author"),
                        discovered_at=datetime.now(timezone.utc),
                        processed=0,
                        metrics=metrics_data,
                        raw_data=r
                    )
                    db.add(content_item)
                    db.flush()
                    added += 1

                snapshot_stmt = insert(MetricSnapshot).values(
                    content_item_id=content_item.id,
                    source=self.NAME,
                    external_id=r["model_id"],
                    category=r["category"] or "",
                    rank=r["rank"],
                    metrics=metrics_data,
                    captured_date=today,
                    captured_at=now,
                ).on_conflict_do_update(
                    # ADR-0002: one row per (source, external_id, captured_date).
                    # A model may appear under multiple HF pipeline tags, but we keep a
                    # single daily row per model (classification fixed to the first seen).
                    index_elements=["source", "external_id", "captured_date"],
                    set_={
                        "content_item_id": content_item.id,
                        "rank": r["rank"],
                        "metrics": metrics_data,
                        "captured_at": now,
                    }
                )
                db.execute(snapshot_stmt)

            db.commit()
            return added
        finally:
            db.close()

    def configs(self):
        configs = []
        for key in HuggingFaceClient.PIPELINE_MAP.keys():
            # 热门：按 trendingScore 排序
            configs.append(HuggingFaceConfig(
                category_key=key,
                limit=100,
                sort="trendingScore",
                search_type="hf_hot",
            ))
            # 更新趋势：按 lastModified 倒序（ADR-0001 §4）
            configs.append(HuggingFaceConfig(
                category_key=key,
                limit=100,
                sort="lastModified",
                search_type="hf_trend",
            ))
        return configs

async def main():
    config = HuggingFaceConfig(
        category_key="文字生成"
    )
    collector = HuggingFaceCollector()
    await collector.fetch(config)

if __name__ == '__main__':
    asyncio.run(main())