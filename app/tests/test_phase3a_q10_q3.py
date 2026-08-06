"""Phase 3A Q10/Q3: news dedup clustering + source_quotes persistence (mocked DB).

Verifies:
- NewsCollector._deduplicate groups exact-url and title-similarity (>=0.85) duplicates.
- NewsAnalyzer prompt requires source_quotes + no-fabrication constraint.
- processor persists source_quotes into Summary (mocked).
"""
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


def _news_item(url, title, src="news", ext="x1"):
    return {
        "source": src, "external_id": ext, "title": title, "url": url,
        "description": "", "author": None, "published_at": None,
        "category": "ai", "raw_data": {},
    }


def test_dedup_exact_url_groups():
    from app.collectors.news_collector import NewsCollector
    c = NewsCollector()
    items = [
        _news_item("https://example.com/a", "Same Story", ext="1"),
        _news_item("https://example.com/a/", "Same Story", ext="2"),  # trailing slash normalized
        _news_item("https://other.com/b", "Different Story", ext="3"),
    ]
    unique = c._deduplicate(items)
    assert len(unique) == 2
    # the two same-url items collapsed into one cluster
    assert len(c._last_clusters) == 1
    assert set(c._last_clusters[0]) == {0, 1}


def test_dedup_title_similarity_groups():
    from app.collectors.news_collector import NewsCollector
    c = NewsCollector()
    items = [
        _news_item("https://a.com/1", "OpenAI releases GPT-5 with new reasoning", ext="1"),
        _news_item("https://b.com/2", "OpenAI releases GPT-5 with new reasoning mode", ext="2"),
        _news_item("https://c.com/3", "Google unveils Gemini 3 model", ext="3"),
    ]
    unique = c._deduplicate(items)
    # first two titles are >=0.85 similar -> 1 cluster; third is distinct
    assert len(unique) == 2
    assert len(c._last_clusters) == 1


def test_dedup_disabled_returns_all():
    from app.collectors.news_collector import NewsCollector
    from app.config import settings
    with patch.object(settings, "news_dedup_enabled", False):
        c = NewsCollector()
        items = [_news_item("https://a.com/1", "T", ext="1"), _news_item("https://a.com/1", "T", ext="2")]
        unique = c._deduplicate(items)
        assert len(unique) == 2


def test_news_analyzer_prompt_requires_source_quotes():
    import inspect
    from app.collectors.news_analyzer import NewsAnalyzer
    src = inspect.getsource(NewsAnalyzer.build_context)
    assert "source_quotes" in src
    assert ("不得臆测" in src) or ("不可编造" in src)
    assert "逐字摘录" in src


@pytest.mark.asyncio
async def test_processor_persists_source_quotes(monkeypatch):
    from app.ai import processor as proc_mod
    from app.database.models import Summary, ContentItem

    fake_summary = MagicMock(spec=Summary)
    captured = {}

    def fake_merge(obj):
        captured["summary"] = obj
        return obj
    fake_db = MagicMock()
    fake_db.merge.side_effect = fake_merge
    fake_db.commit.return_value = None

    monkeypatch.setattr(proc_mod, "SessionLocal", lambda: fake_db)
    # build a fake summarizer returning source_quotes
    fake_summarizer = AsyncMock()
    fake_summarizer.summarize.return_value = {
        "one_liner": "x", "summary": "y", "use_cases": "", "pros": [], "cons": [],
        "score": 8, "tags": [], "install_step": "", "maturity_level": "突发新闻",
        "source_quotes": ["原文摘录一", "原文摘录二"], "model": "test", "token_cost": 0.0,
    }
    fake_analyzer = AsyncMock()
    fake_analyzer.build_context.return_value = "prompt"

    bs = proc_mod.BatchSummary.__new__(proc_mod.BatchSummary)
    bs.analyzer = fake_analyzer
    bs.summarizer = fake_summarizer
    bs.provider = "test"

    fake_item = MagicMock(spec=ContentItem)
    fake_item.id = 99
    fake_item.title = "T"
    fake_item.url = "https://x"

    import asyncio
    await bs._process_single(fake_summarizer, fake_item, asyncio.Semaphore(1))
    assert captured["summary"].source_quotes == ["原文摘录一", "原文摘录二"]
