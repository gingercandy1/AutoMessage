"""TDD S2 (regression): opencode provider actually issues a POST to <opencode_base_url>/chat/completions
and parses the returned content into a dict — without any network call.

Tests the real behavior seam of summarize() via injected candidates, not config assembly.
"""
import json
import pytest
from unittest.mock import MagicMock

from app.ai.summarizer import ProjectSummarizer, ProviderCandidate


@pytest.mark.asyncio
async def test_opencode_summarize_posts_to_compatible_endpoint():
    base = "http://localhost:2424/v1"
    captured = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": json.dumps({
                    "one_liner": "测试模型", "summary": "desc", "score": 8,
                    "tags": ["llm"], "pros": [], "cons": []
                })}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

    class _FakeClient:
        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

        async def aclose(self):
            return None

    candidates = [ProviderCandidate("opencode", "claude-opus-4", base, "sk-test")]
    s = ProjectSummarizer(candidates=candidates)
    s.client = _FakeClient()

    result = await s.summarize("分析这个项目")

    # 行为断言：请求打到 opencode 兼容端点、带 Bearer、model 正确
    assert captured["url"] == f"{base}/chat/completions"
    assert captured["json"]["model"] == "claude-opus-4"
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    # 响应被解析为 dict 且包含结构化字段
    assert isinstance(result, dict)
    assert result.get("one_liner") == "测试模型"
    assert result.get("score") == 8
    assert result.get("provider") == "opencode"
    assert result.get("parse_failed") is not True
