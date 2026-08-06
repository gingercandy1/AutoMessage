"""Phase 2 Q2: HuggingFace "update trend" collection (ADR-0001 §4).

Verifies the collector emits BOTH a popular config (trendingScore) and an
update-trend config (lastModified desc, search_type=hf_trend) per pipeline category,
without touching a real database.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.collectors.huggingface_collector import HuggingFaceCollector, HuggingFaceConfig


def test_configs_contains_trend_and_hot_per_category():
    collector = HuggingFaceCollector(client=AsyncMock())
    configs = collector.configs()
    # Each pipeline category should yield 2 configs: hot + trend
    hot = [c for c in configs if c.search_type == "hf_hot"]
    trend = [c for c in configs if c.search_type == "hf_trend"]
    assert hot, "expected hf_hot configs"
    assert trend, "expected hf_trend (update trend) configs"
    assert len(hot) == len(trend), "hot/trend counts must match per category"
    # trend config uses lastModified sort
    assert all(c.sort == "lastModified" for c in trend)
    assert all(c.sort == "trendingScore" for c in hot)


@pytest.mark.asyncio
async def test_fetch_passes_sort_to_client(monkeypatch):
    collector = HuggingFaceCollector(client=AsyncMock())
    cfg = HuggingFaceConfig(category_key="文字生成", sort="lastModified", search_type="hf_trend")
    # Patch the client method to avoid network; return a minimal model list
    collector.client.fetch_by_category = AsyncMock(return_value=[
        {"id": "org/model", "tags": ["text-generation"], "likes": 1, "downloads": 2,
         "trendingScore": 0.1, "createdAt": "2024-01-01", "library_name": "transformers",
         "private": False, "lastModified": "2024-06-01T00:00:00Z"}
    ])
    # Patch save to avoid DB
    async def fake_save(records):
        return len(records)
    monkeypatch.setattr(collector, "save", fake_save)

    added = await collector.fetch(cfg)
    assert added == 1
    # sort param forwarded to the client
    _, kwargs = collector.client.fetch_by_category.call_args
    assert collector.client.fetch_by_category.call_args.kwargs.get("sort") == "lastModified"
