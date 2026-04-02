"""Concrete ModelClient implementations for all supported LLM providers.

Selected via RECALL_LLM_PROVIDER env var:
  openrouter  — cloud, needs OPENROUTER_API_KEY
  ollama      — local, zero cloud deps, fully offline
  vllm        — local GPU, OpenAI-compatible API
  claude      — Anthropic API, needs ANTHROPIC_API_KEY
  lmstudio    — local, LM Studio desktop app

Apache 2.0 — original implementation.
"""
from __future__ import annotations

from typing import Any

from recall.core.model_client import BaseModelClient
from recall.errors import LLMProviderError, ProviderNotConfiguredError
from recall.logging import get_logger

log = get_logger(__name__)


# ── Factory ───────────────────────────────────────────────────────────────────

def get_client_for_provider(provider: str) -> BaseModelClient:
    """Return the correct ModelClient subclass for the given provider string."""
    mapping: dict[str, type[BaseModelClient]] = {
        "openrouter": OpenRouterClient,
        "ollama": OllamaClient,
        "vllm": VLLMClient,
        "claude": ClaudeClient,
        "lmstudio": LMStudioClient,
    }
    cls = mapping.get(provider.lower())
    if cls is None:
        raise ProviderNotConfiguredError(
            f"Unknown RECALL_LLM_PROVIDER='{provider}'. "
            f"Valid values: {', '.join(mapping)}"
        )
    return cls()


# ── OpenRouter ────────────────────────────────────────────────────────────────

class OpenRouterClient(BaseModelClient):
    """Routes to any model via OpenRouter's OpenAI-compatible API."""

    def __init__(self) -> None:
        from recall.config import (
            OPENROUTER_API_KEY,
            OPENROUTER_BASE_URL,
            OPENROUTER_DEFAULT_MODEL,
            RECALL_LLM_MODEL,
        )
        if not OPENROUTER_API_KEY:
            raise ProviderNotConfiguredError(
                "OPENROUTER_API_KEY is not set.",
                recovery_hint="Add OPENROUTER_API_KEY to your .env file.",
            )
        self._api_key = OPENROUTER_API_KEY
        self._base_url = OPENROUTER_BASE_URL
        self._default_model = RECALL_LLM_MODEL or OPENROUTER_DEFAULT_MODEL

    async def chat_completion(
        self, messages: list[dict], model: str = "", **kwargs: Any
    ) -> str:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise LLMProviderError("openai package not installed. Run: uv add openai")
        client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        try:
            resp = await client.chat.completions.create(
                model=model or self._default_model,
                messages=messages,
                **kwargs,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            raise LLMProviderError(f"OpenRouter request failed: {exc}") from exc


# ── Ollama ────────────────────────────────────────────────────────────────────

class OllamaClient(BaseModelClient):
    """Calls a local Ollama instance. Zero cloud dependencies."""

    def __init__(self) -> None:
        from recall.config import OLLAMA_DEFAULT_MODEL, OLLAMA_HOST, RECALL_LLM_MODEL
        self._host = OLLAMA_HOST
        self._default_model = RECALL_LLM_MODEL or OLLAMA_DEFAULT_MODEL

    async def chat_completion(
        self, messages: list[dict], model: str = "", **kwargs: Any
    ) -> str:
        try:
            import httpx
        except ImportError:
            raise LLMProviderError("httpx not installed. Run: uv add httpx")
        _model = model or self._default_model
        payload = {"model": _model, "messages": messages, "stream": False, **kwargs}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{self._host}/api/chat", json=payload)
                resp.raise_for_status()
                return resp.json().get("message", {}).get("content", "")
        except Exception as exc:
            raise LLMProviderError(
                f"Ollama request failed ({self._host}): {exc}",
                recovery_hint="Ensure Ollama is running: `ollama serve`",
            ) from exc


# ── vLLM ──────────────────────────────────────────────────────────────────────

class VLLMClient(BaseModelClient):
    """Calls a local vLLM server (OpenAI-compatible API)."""

    def __init__(self) -> None:
        from recall.config import RECALL_LLM_MODEL, VLLM_DEFAULT_MODEL, VLLM_HOST, VLLM_PORT
        self._base_url = f"http://{VLLM_HOST}:{VLLM_PORT}/v1"
        self._default_model = RECALL_LLM_MODEL or VLLM_DEFAULT_MODEL

    async def chat_completion(
        self, messages: list[dict], model: str = "", **kwargs: Any
    ) -> str:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise LLMProviderError("openai package not installed. Run: uv add openai")
        client = AsyncOpenAI(api_key="vllm", base_url=self._base_url)
        try:
            resp = await client.chat.completions.create(
                model=model or self._default_model,
                messages=messages,
                **kwargs,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            raise LLMProviderError(
                f"vLLM request failed ({self._base_url}): {exc}",
                recovery_hint="Ensure vLLM server is running: `vllm serve <model>`",
            ) from exc


# ── Claude (Anthropic SDK) ────────────────────────────────────────────────────

class ClaudeClient(BaseModelClient):
    """Calls the Anthropic API using the official SDK."""

    def __init__(self) -> None:
        from recall.config import ANTHROPIC_API_KEY, ANTHROPIC_DEFAULT_MODEL, RECALL_LLM_MODEL
        if not ANTHROPIC_API_KEY:
            raise ProviderNotConfiguredError(
                "ANTHROPIC_API_KEY is not set.",
                recovery_hint="Add ANTHROPIC_API_KEY to your .env file.",
            )
        self._api_key = ANTHROPIC_API_KEY
        self._default_model = RECALL_LLM_MODEL or ANTHROPIC_DEFAULT_MODEL

    async def chat_completion(
        self, messages: list[dict], model: str = "", **kwargs: Any
    ) -> str:
        try:
            import anthropic
        except ImportError:
            raise LLMProviderError("anthropic package not installed. Run: uv add anthropic")
        # Convert OpenAI-format → Anthropic format
        system_content = ""
        anthropic_messages: list[dict] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_content = content
            else:
                a_role = "assistant" if role == "assistant" else "user"
                anthropic_messages.append({"role": a_role, "content": content})
        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        try:
            kwargs.pop("stream", None)
            max_tokens = kwargs.pop("max_tokens", 8096)
            sys_param = system_content if system_content else anthropic.NOT_GIVEN
            resp = await client.messages.create(
                model=model or self._default_model,
                messages=anthropic_messages,
                system=sys_param,
                max_tokens=max_tokens,
                **kwargs,
            )
            return resp.content[0].text if resp.content else ""
        except Exception as exc:
            raise LLMProviderError(f"Anthropic request failed: {exc}") from exc


# ── LM Studio ─────────────────────────────────────────────────────────────────

class LMStudioClient(BaseModelClient):
    """Calls a local LM Studio instance (OpenAI-compatible API)."""

    def __init__(self) -> None:
        from recall.config import LMSTUDIO_DEFAULT_MODEL, LMSTUDIO_HOST, RECALL_LLM_MODEL
        self._base_url = f"{LMSTUDIO_HOST}/v1"
        self._default_model = RECALL_LLM_MODEL or LMSTUDIO_DEFAULT_MODEL

    async def chat_completion(
        self, messages: list[dict], model: str = "", **kwargs: Any
    ) -> str:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise LLMProviderError("openai package not installed. Run: uv add openai")
        client = AsyncOpenAI(api_key="lmstudio", base_url=self._base_url)
        try:
            _model = model or self._default_model or "local-model"
            resp = await client.chat.completions.create(
                model=_model, messages=messages, **kwargs
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            raise LLMProviderError(
                f"LM Studio request failed ({self._base_url}): {exc}",
                recovery_hint="Ensure LM Studio is running with a model loaded.",
            ) from exc
