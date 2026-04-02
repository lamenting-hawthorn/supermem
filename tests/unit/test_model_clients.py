"""Unit tests for ModelClient implementations — mocked HTTP."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from recall.model.base import (
    ClaudeClient,
    LMStudioClient,
    OllamaClient,
    OpenRouterClient,
    VLLMClient,
    get_client_for_provider,
)
from recall.errors import ProviderNotConfiguredError


MESSAGES = [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# OpenRouterClient
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import recall.config as cfg
    monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "test-key")

    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="hi from openrouter"))]

    with patch("openai.AsyncOpenAI") as mock_cls:
        instance = mock_cls.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        client = OpenRouterClient()
        result = await client.chat_completion(MESSAGES, model="openai/gpt-4o-mini")

    assert result == "hi from openrouter"


def test_openrouter_raises_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Need to reload config so it picks up cleared env var
    import importlib
    import recall.config as cfg
    monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "")
    with pytest.raises(ProviderNotConfiguredError):
        OpenRouterClient()


# ---------------------------------------------------------------------------
# OllamaClient — mock httpx
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ollama_returns_text() -> None:
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "hi from ollama"}}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_cm

        client = OllamaClient()
        result = await client.chat_completion(MESSAGES, model="llama3")

    assert result == "hi from ollama"


# ---------------------------------------------------------------------------
# VLLMClient — mock openai
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vllm_returns_text() -> None:
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="hi from vllm"))]

    with patch("openai.AsyncOpenAI") as mock_cls:
        instance = mock_cls.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        client = VLLMClient()
        result = await client.chat_completion(MESSAGES, model="mistral-7b")

    assert result == "hi from vllm"


# ---------------------------------------------------------------------------
# LMStudioClient — mock openai
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lmstudio_returns_text() -> None:
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="hi from lmstudio"))]

    with patch("openai.AsyncOpenAI") as mock_cls:
        instance = mock_cls.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        client = LMStudioClient()
        result = await client.chat_completion(MESSAGES, model="local-model")

    assert result == "hi from lmstudio"


# ---------------------------------------------------------------------------
# ClaudeClient — mock anthropic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claude_converts_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic", reason="anthropic package not installed")
    import recall.config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "test-anthropic-key")

    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="hi from claude")]

    messages_with_system = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hello"},
    ]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        instance = mock_cls.return_value
        instance.messages.create = AsyncMock(return_value=mock_resp)

        client = ClaudeClient()
        result = await client.chat_completion(messages_with_system, model="claude-haiku-4-5-20251001")

    assert result == "hi from claude"


def test_claude_raises_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic", reason="anthropic package not installed")
    import recall.config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    with pytest.raises(ProviderNotConfiguredError):
        ClaudeClient()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def test_get_client_for_provider_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    import recall.config as cfg
    monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "fake-key")
    client = get_client_for_provider("openrouter")
    assert isinstance(client, OpenRouterClient)


def test_get_client_for_provider_ollama() -> None:
    client = get_client_for_provider("ollama")
    assert isinstance(client, OllamaClient)


def test_get_client_for_provider_unknown_raises() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        get_client_for_provider("nonexistent_provider")
