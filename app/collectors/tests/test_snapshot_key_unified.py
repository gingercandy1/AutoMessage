"""Verify ADR-0002: MetricSnapshot daily key is (source, external_id, captured_date),
i.e. `category` is removed from the uniqueness key and collectors upsert one row
per entity per day (no category split).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from app.collectors.github_collector import GitHubCollector
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
            return orig_ocdu(index_elements=index_elements, set_=set_, **kw)

        stmt.on_conflict_do_update = spy_on_conflict
        return stmt

    monkeypatch.setattr("app.collectors.github_collector.insert", fake_insert)
    monkeypatch.setattr("app.collectors.huggingface_collector.insert", fake_insert)
    return captured


def _fake_session(monkeypatch, fake_items):
    """Patch SessionLocal to return a MagicMock session that yields fake_items on query."""
    session = MagicMock()
    # query(...).filter_by(...).first() -> first fake item or None
    query_mock = MagicMock()
    query_mock.filter_by.return_value = query_mock
    query_mock.first.return_value = fake_items
    session.query.return_value = query_mock
    monkeypatch.setattr("app.collectors.github_collector.SessionLocal", lambda: session)
    monkeypatch.setattr("app.collectors.huggingface_collector.SessionLocal", lambda: session)
    return session


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
    # Also confirm the snapshot row was executed (not silently dropped)
    assert session.execute.called


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
