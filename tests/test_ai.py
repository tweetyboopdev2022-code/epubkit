"""Providers: request shape, error translation, and retry behaviour.

No network. Each provider's `_post` is replaced, which is the only part that
touches the wire.
"""
from __future__ import annotations

import urllib.error

import pytest

from kobokit.ai import AIError, AIUnavailable, build
from kobokit.ai.providers import AnthropicProvider, OllamaProvider, OpenAIProvider
from kobokit.config import AIConfig


def test_anthropic_request_and_parse(monkeypatch):
    seen = {}

    def fake(url, payload, headers):
        seen.update(url=url, payload=payload, headers=headers)
        return {"content": [{"type": "text", "text": "hello"}],
                "model": "claude-x",
                "usage": {"input_tokens": 10, "output_tokens": 2}}

    p = AnthropicProvider("claude-x", "https://api.anthropic.com", "sk-1")
    monkeypatch.setattr(p, "_post", fake)
    out = p.complete("hi", system="be brief")

    assert out.text == "hello"
    assert out.input_tokens == 10
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "sk-1"
    assert seen["payload"]["system"] == "be brief"


def test_openai_request_and_parse(monkeypatch):
    seen = {}

    def fake(url, payload, headers):
        seen.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": "hi there"}}],
                "model": "gpt", "usage": {}}

    p = OpenAIProvider("gpt", "https://api.openai.com", "sk-2")
    monkeypatch.setattr(p, "_post", fake)
    assert p.complete("x").text == "hi there"
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer sk-2"


def test_openai_base_url_not_doubled(monkeypatch):
    seen = {}
    p = OpenAIProvider("m", "http://local/v1", None)
    monkeypatch.setattr(p, "_post",
                        lambda u, pl, h: seen.update(url=u) or
                        {"choices": [{"message": {"content": "k"}}]})
    p.complete("x")
    assert seen["url"] == "http://local/v1/chat/completions"


def test_ollama_missing_model_is_unavailable_not_failure(monkeypatch):
    """Ollama answers HTTP 200 with an error body when a model is absent."""
    p = OllamaProvider("llama3.1:latest", "http://localhost:11434")
    monkeypatch.setattr(p, "_post", lambda *a: {"error": "model 'x' not found, try pulling it"})
    with pytest.raises(AIUnavailable, match="ollama pull"):
        p.complete("x")


def test_unavailable_is_not_retried(monkeypatch):
    calls = []

    def fake(*a):
        calls.append(1)
        raise AIUnavailable("nope")

    p = OllamaProvider("m", "http://localhost:11434")
    monkeypatch.setattr(p, "_post", fake)
    with pytest.raises(AIUnavailable):
        p.complete("x", retries=3)
    assert len(calls) == 1          # retrying a missing model is futile


def test_transient_failure_is_retried(monkeypatch):
    calls = []

    def fake(*a):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("boom")
        return {"response": "recovered"}

    p = OllamaProvider("m", "http://localhost:11434")
    monkeypatch.setattr(p, "_post", fake)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    assert p.complete("x", retries=3).text == "recovered"
    assert len(calls) == 3


def test_exhausted_retries_raise(monkeypatch):
    p = OllamaProvider("m", "http://localhost:11434")
    monkeypatch.setattr(p, "_post", lambda *a: (_ for _ in ()).throw(AIError("x")))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(AIError, match="after 2 attempts"):
        p.complete("x", retries=2)


@pytest.mark.parametrize("provider,cls", [
    ("anthropic", AnthropicProvider),
    ("openai", OpenAIProvider),
    ("ollama", OllamaProvider),
])
def test_factory_builds_each(provider, cls, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert isinstance(build(AIConfig(provider=provider)), cls)


def test_factory_rejects_unknown():
    with pytest.raises(AIError, match="unknown provider"):
        build(AIConfig(provider="nope"))
