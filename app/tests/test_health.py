"""Unit test for get_pipeline_health() (ADR-0001 §7).

Does not touch a real database: the session factory is patched with a MagicMock
that returns canned rows, so we assert (a) it never raises and (b) the returned
dict has the expected structure and computes success rate correctly.
"""
import pytest
from unittest.mock import MagicMock, patch


def _fake_session(rows, snap_rows):
    session = MagicMock()
    query_mock = MagicMock()
    # ContentItem query: group_by(...).all() -> rows
    query_mock.group_by.return_value = query_mock
    query_mock.all.side_effect = [rows, snap_rows]
    session.query.return_value = query_mock
    return session


@pytest.mark.asyncio
async def test_health_returns_structure_and_rate(monkeypatch):
    # rows = (source, processed_state, count)
    rows = [
        ("github", 0, 2),   # unprocessed
        ("github", 1, 7),   # ok
        ("github", 2, 1),   # error
        ("huggingface", 1, 5),
    ]
    # snap_rows = (source, last_captured_at)
    snap_rows = [("github", None), ("huggingface", None)]

    fake = _fake_session(rows, snap_rows)
    monkeypatch.setattr("app.health.SessionLocal", lambda: fake)

    from app.health import get_pipeline_health
    h = get_pipeline_health()

    assert "per_source" in h
    assert "overall_success_rate_pct" in h
    # total items = 2+7+1+5 = 15, errors = 1 -> 14/15 = 93.33%
    assert h["total_items"] == 15
    assert abs(h["overall_success_rate_pct"] - 93.33) < 0.1
    assert h["per_source"]["github"]["ok"] == 7
    assert h["per_source"]["github"]["error"] == 1


@pytest.mark.asyncio
async def test_health_never_raises_on_db_error(monkeypatch):
    fake = MagicMock()
    fake.query.side_effect = RuntimeError("db down")
    monkeypatch.setattr("app.health.SessionLocal", lambda: fake)

    from app.health import get_pipeline_health
    h = get_pipeline_health()
    # Must return gracefully with error info, not raise
    assert h["error"] is not None
    assert h["total_items"] == 0
