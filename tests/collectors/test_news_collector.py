import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.collectors.news_collector import NewsCollector
from app.api.hackernews_client import HackerNewsClient, HackerNewsAPIError
from app.api.rss_client import RSSClient, RSSFeedEntry
from datetime import datetime, timezone


class TestNewsCollector:
    """测试 NewsCollector"""

    @pytest.fixture
    def mock_hn_client(self):
        client = AsyncMock(spec=HackerNewsClient)
        return client

    @pytest.fixture
    def mock_rss_client(self):
        client = AsyncMock(spec=RSSClient)
        return client

    @pytest.fixture
    def collector(self, mock_hn_client, mock_rss_client):
        collector = NewsCollector()
        collector.hn_client = mock_hn_client
        collector.rss_client = mock_rss_client
        return collector

    def test_matches_keywords(self, collector):
        """测试关键词匹配"""
        # 使用真实的关键词列表（包含短语）
        keywords = ["ai", "machine learning", "llm", "gpt"]
        assert collector._matches_keywords("New AI model released", keywords) is True
        assert collector._matches_keywords("Machine learning tutorial", keywords) is True
        assert collector._matches_keywords("JavaScript framework", keywords) is False
        assert collector._matches_keywords("", keywords) is False

    def test_categorize_news(self, collector):
        """测试新闻分类"""
        # 研究论文
        assert collector._categorize_news("New paper on transformers", "arXiv preprint", "news") == "research"
        
        # LLM
        assert collector._categorize_news("GPT-5 released", "New LLM from OpenAI", "news") == "llm"
        assert collector._categorize_news("Llama 3 benchmarks", "Fine-tuning results", "news") == "llm"
        
        # AI 通用
        assert collector._categorize_news("AI breakthrough", "Deep learning advances", "news") == "ai"
        
        # 工具
        assert collector._categorize_news("New ML framework", "Open source library release", "news") == "tools"
        
        # 行业 - 注意：避免包含 "ai" 子串的词汇
        assert collector._categorize_news("Startup gets funding", "Firm bought", "news") == "industry"
        
        # 默认
        assert collector._categorize_news("Random tech news", "Nothing special", "news") == "ai"

    def test_normalize_hn_item_valid(self, collector):
        """测试有效 HN 项目标准化"""
        item = {
            "id": 12345,
            "type": "story",
            "title": "New AI model breaks records",
            "url": "https://example.com/ai-news",
            "score": 100,
            "descendants": 50,
            "by": "user123",
            "time": 1704067200,  # 2024-01-01
            "text": "This is a great AI article about LLMs",
        }
        result = collector._normalize_hn_item(item)
        assert result is not None
        assert result["source"] == "hacker_news"
        assert result["external_id"] == "hn_12345"
        assert result["title"] == "New AI model breaks records"
        assert result["hn_score"] == 100
        assert result["hn_comments"] == 50
        assert result["category"] == "llm"

    def test_normalize_hn_item_low_score(self, collector):
        """测试低分 HN 项目被过滤"""
        item = {
            "id": 12345,
            "type": "story",
            "title": "Random news",
            "url": "https://example.com",
            "score": 10,  # 低于默认阈值 50
            "by": "user123",
            "time": 1704067200,
        }
        result = collector._normalize_hn_item(item)
        assert result is None

    def test_normalize_hn_item_no_keywords(self, collector):
        """测试无关键词 HN 项目被过滤"""
        item = {
            "id": 12345,
            "type": "story",
            "title": "JavaScript framework released",
            "url": "https://example.com",
            "score": 100,
            "by": "user123",
            "time": 1704067200,
            "text": "New React version",
        }
        result = collector._normalize_hn_item(item)
        assert result is None

    def test_normalize_hn_item_non_story(self, collector):
        """测试非 story 类型被过滤"""
        item = {
            "id": 12345,
            "type": "comment",
            "title": "Comment on story",
            "score": 100,
        }
        result = collector._normalize_hn_item(item)
        assert result is None

    def test_normalize_rss_entry_valid(self, collector):
        """测试有效 RSS 条目标准化"""
        entry = RSSFeedEntry(
            title="AI Research Paper Published",
            url="https://arxiv.org/abs/1234.5678",
            description="New transformer architecture",
            author="John Doe",
            published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            feed_url="https://export.arxiv.org/rss/cs.AI",
            content="Full paper content...",
            tags=["machine-learning", "transformers"],
        )
        result = collector._normalize_rss_entry(entry, "news")
        assert result is not None
        assert result["source"] == "news"
        assert result["title"] == "AI Research Paper Published"
        assert result["category"] == "research"
        assert result["tags"] == ["machine-learning", "transformers"]

    def test_normalize_rss_entry_missing_title(self, collector):
        """测试缺少标题的 RSS 条目被过滤"""
        entry = RSSFeedEntry(
            title="",
            url="https://example.com",
            description="Description",
            author=None,
            published_at=None,
            feed_url="https://example.com/feed",
        )
        result = collector._normalize_rss_entry(entry, "news")
        assert result is None

    def test_deduplicate_url(self, collector):
        """测试 URL 去重（标题不同 → 仅按归一化 URL 去重）"""
        items = [
            {"url": "https://example.com/1", "title": "Alpha release announced"},
            {"url": "https://example.com/2", "title": "Beta framework launched"},
            {"url": "https://example.com/1/", "title": "Alpha release announced"},  # 尾部斜杠归一化
        ]
        unique = collector._deduplicate(items)
        assert len(unique) == 2
        assert unique[0]["url"] == "https://example.com/1"
        assert unique[1]["url"] == "https://example.com/2"

    def test_deduplicate_title_similarity(self, collector):
        """测试标题相似度去重（Q10：Levenshtein >= 0.85 视为同簇）"""
        items = [
            {"url": "https://example.com/1", "title": "OpenAI releases GPT-5 model"},
            {"url": "https://example.com/2", "title": "OpenAI releases GPT-5 models"},  # 仅差一个 s
        ]
        unique = collector._deduplicate(items)
        assert len(unique) == 1  # 标题高度相似 → 归入同一簇

    @pytest.mark.asyncio
    async def test_enrich_full_text_reuses_rss_content(self, collector):
        """Q3: RSS 条目已有 content 时，直接写入 raw_data['full_text']，不再联网。"""
        items = [{
            "url": "https://blog.example.com/post",
            "title": "Post",
            "raw_data": {"content": "<p>Full article body here.</p>"},
        }]
        # _fetch_full_text 不应被调用（RSS content 已足够）
        with patch.object(collector, "_fetch_full_text", new=AsyncMock()) as mocked:
            await collector._enrich_full_text(items)
        mocked.assert_not_awaited()
        assert items[0]["raw_data"]["full_text"] == "<p>Full article body here.</p>"

    @pytest.mark.asyncio
    async def test_enrich_full_text_fetches_when_missing(self, collector):
        """Q3: 无缓存正文时，按 URL 抓取并写入 raw_data['full_text']。"""
        items = [{"url": "https://news.example.com/a", "title": "A", "raw_data": {}}]
        with patch.object(
            collector, "_fetch_full_text", new=AsyncMock(return_value="Extracted body text.")
        ) as mocked:
            await collector._enrich_full_text(items)
        mocked.assert_awaited_once_with("https://news.example.com/a")
        assert items[0]["raw_data"]["full_text"] == "Extracted body text."

    @pytest.mark.asyncio
    async def test_enrich_full_text_fetch_failure_is_safe(self, collector):
        """Q3: 抓取失败不抛出、不阻断，raw_data 保持原样。"""
        items = [{"url": "https://bad.example.com", "title": "B", "raw_data": {"x": 1}}]
        with patch.object(
            collector, "_fetch_full_text", new=AsyncMock(return_value="")
        ):
            await collector._enrich_full_text(items)  # must not raise
        assert "full_text" not in items[0]["raw_data"]



class TestHackerNewsClient:
    """测试 HackerNewsClient"""

    @pytest.mark.asyncio
    async def test_get_top_stories(self):
        client = HackerNewsClient()
        with patch.object(client, '_get_json', return_value=[1, 2, 3, 4, 5]):
            stories = await client.get_top_stories(limit=5)
            assert stories == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_get_item(self):
        client = HackerNewsClient()
        with patch.object(client, '_get_json', return_value={"id": 123, "title": "Test"}):
            item = await client.get_item(123)
            assert item["id"] == 123
            assert item["title"] == "Test"

    @pytest.mark.asyncio
    async def test_get_items_batch(self):
        client = HackerNewsClient()
        mock_items = [
            {"id": 1, "title": "Item 1"},
            {"id": 2, "title": "Item 2"},
        ]
        with patch.object(client, 'get_item', side_effect=mock_items):
            items = await client.get_items_batch([1, 2])
            assert len(items) == 2
            assert items[0]["title"] == "Item 1"
            assert items[1]["title"] == "Item 2"


class TestRSSClient:
    """测试 RSSClient"""

    @pytest.fixture
    def rss_client(self):
        return RSSClient()

    def test_parse_entry_valid(self, rss_client):
        """测试解析有效 RSS 条目"""
        import feedparser
        entry = feedparser.FeedParserDict({
            "title": "Test Article",
            "link": "https://example.com/article",
            "summary": "Article summary",
            "author": "Author Name",
            "published_parsed": (2024, 1, 1, 12, 0, 0, 0, 1, 0),
            "tags": [{"term": "ai"}, {"term": "ml"}],
            "content": [{"value": "Full content"}],
        })
        result = rss_client._parse_entry(entry, "https://example.com/feed")
        assert result is not None
        assert result.title == "Test Article"
        assert result.url == "https://example.com/article"
        assert result.author == "Author Name"
        assert result.tags == ["ai", "ml"]
        assert result.content == "Full content"

    def test_parse_entry_missing_title(self, rss_client):
        """测试缺少标题的条目返回 None"""
        import feedparser
        entry = feedparser.FeedParserDict({
            "link": "https://example.com/article",
            "summary": "Article summary",
        })
        result = rss_client._parse_entry(entry, "https://example.com/feed")
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])