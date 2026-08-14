"""Verify ADR-0002: MetricSnapshot daily key is (source, external_id, captured_date),
i.e. `category` is removed from the uniqueness key and collectors upsert one row
per entity per day (no category split).

Covers BOTH GitHub collectors (GitHubCollector and GitHubTrendingCollector) — the
trending one was the source of a real regression (4-column key + datetime.date()
for captured_at) that the 6h snapshot job hit with IntegrityError.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.collectors.github_collector import GitHubCollector, GitHubTrendingCollector
from app.collectors.huggingface_collector import HuggingFaceCollector
from sqlalchemy.dialects.postgresql import insert


EXPECTED_KEY = ["source", "external_id", "captured_date"]


def _capture_index_elements(monkeypatch):
    """Patch insert() so we can inspect the on_conflict_do_update index_elements."""
    captured = {}

    def fake_insert(table):
        stmt = insert(table)
        orig_ocdu = stmt.on_conflict_do_update

        def spy_on_conflict(index_elements=None, set_=None, **kw):
            captured["index_elements"] = list(index_elements)
            captured["set_keys"] = list(set_.keys())
            return orig_ocdu(index_elements=index_elements, set_=set_, **kw)

        stmt.on_conflict_do_update = spy_on_conflict
        return stmt

    for mod in (
        "app.collectors.github_collector",
        "app.collectors.huggingface_collector",
    ):
        monkeypatch.setattr(f"{mod}.insert", fake_insert)
    return captured


def _fake_session(monkeypatch, fake_items):
    """Patch SessionLocal to return a MagicMock session that yields fake_items on query."""
    session = MagicMock()
    query_mock = MagicMock()
    query_mock.filter_by.return_value = query_mock
    query_mock.first.return_value = fake_items
    session.query.return_value = query_mock
    monkeypatch.setattr("app.collectors.github_collector.SessionLocal", lambda: session)
    monkeypatch.setattr("app.collectors.huggingface_collector.SessionLocal", lambda: session)
    return session


class _FakeContentItem:
    """Minimal ContentItem stand-in for GitHubTrendingCollector.save (which does
    db.add(item) for new rows, so `item` must expose these attributes)."""

    def __init__(self, external_id, language="python"):
        self.id = 1
        self.external_id = external_id
        self.title = external_id
        self.url = f"https://github.com/{external_id}"
        self.description = "d"
        self.author = external_id.split("/")[0]
        self.raw_data = {}
        self.metrics = {"language": language, "search_type": "trending"}

    def get(self, key, default=None):
        return getattr(self, key, default)


@pytest.mark.asyncio
async def test_github_save_uses_unified_key(monkeypatch):
    captured = _capture_index_elements(monkeypatch)
    session = _fake_session(monkeypatch, None)

    collector = GitHubCollector(client=AsyncMock())
    ranked = [{
        "full_name": "owner/repo", "name": "repo", "url": "https://github.com/owner/repo",
        "description": "d", "owner": "owner", "stargazers_count": 10, "forks_count": 1,
        "watchers_count": 1, "open_issues_count": 0, "fork": False,
        "star_growth": {"recent_stars": 5, "estimated": False},
        "commit_stats": {"commits_last_4_weeks": 3},
        "latest_release": None, "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z", "language": "python",
        "license": None, "stars_gained_window_days": 7,
    }]
    await collector.save(ranked)
    assert captured["index_elements"] == EXPECTED_KEY
    assert session.execute.called


@pytest.mark.asyncio
async def test_github_trending_save_uses_unified_key(monkeypatch):
    """Regression guard for the trending collector — it previously used the 4-column
    key (source, external_id, category, captured_date) and stored captured_at as a date,
    breaking the 6h lightweight snapshot. Must match the unified 3-column key and keep
    captured_at as a full datetime."""
    captured = _capture_index_elements(monkeypatch)
    session = _fake_session(monkeypatch, None)

    collector = GitHubTrendingCollector(client=AsyncMock())
    records = [_FakeContentItem("owner/trending-repo", language="python")]
    await collector.save(records)
    assert captured["index_elements"] == EXPECTED_KEY
    # captured_at must remain a full datetime (type preserved), not a date()
    assert "captured_at" in captured["set_keys"]


@pytest.mark.asyncio
async def test_huggingface_save_uses_unified_key(monkeypatch):
    captured = _capture_index_elements(monkeypatch)
    session = _fake_session(monkeypatch, None)

    collector = HuggingFaceCollector(client=AsyncMock())
    records = [{
        "model_id": "org/model", "category": "文字生成", "pipeline_tag": "text-generation",
        "likes": 1, "downloads": 2, "rank": 1, "created_at": "2024-01-01",
        "library_name": "transformers", "trending_score": 0.5, "author": "org",
        "private": False, "description": "d", "license": None, "tags": [],
    }]
    await collector.save(records)
    assert captured["index_elements"] == EXPECTED_KEY
    assert session.execute.called
