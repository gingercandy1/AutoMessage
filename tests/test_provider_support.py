"""Provider support: openrouter (existing) + openai base_url override + opencode alias.

The summarizer was refactored to an adapter/candidate model: each provider is an
adapter whose build_candidates(settings) emits ProviderCandidate(base_url, model, ...).
These tests verify that wiring without any network call.
"""
import pytest

from app.ai.summarizer import (
    ProjectSummarizer,
    ProviderCandidate,
    OpenRouterAdapter,
    OpenAIAdapter,
    OpenCodeAdapter,
)
from app.config import settings


def test_openrouter_adapter_default_base_url(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-test")
    cands = OpenRouterAdapter().build_candidates(settings)
    assert cands
    assert all(c.base_url == "https://openrouter.ai/api/v1" for c in cands)
    assert all(c.provider == "openrouter" for c in cands)


def test_openai_adapter_uses_configurable_base_url(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_base_url", "https://my-proxy.example.com/v1")
    cands = OpenAIAdapter().build_candidates(settings)
    assert cands
    assert cands[0].base_url == "https://my-proxy.example.com/v1"
    assert cands[0].api_key == "sk-test"


def test_opencode_adapter_is_openai_compatible(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "opencode_base_url", "http://localhost:2424/v1")
    monkeypatch.setattr(settings, "opencode_model", "claude-opus-4")
    cands = OpenCodeAdapter().build_candidates(settings)
    assert cands
    # opencode speaks OpenAI-compatible protocol
    assert cands[0].base_url == "http://localhost:2424/v1"
    assert cands[0].model == "claude-opus-4"
    assert cands[0].provider == "opencode"
    assert cands[0].api_key == "sk-test"


def test_opencode_adapter_falls_back_to_openai_model(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "opencode_model", "")
    monkeypatch.setattr(settings, "openai_model", "gpt-4o-mini")
    cands = OpenCodeAdapter().build_candidates(settings)
    assert cands
    assert cands[0].model == "gpt-4o-mini"


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        ProjectSummarizer(provider="gemini")
