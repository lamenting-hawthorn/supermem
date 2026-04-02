"""BaseModelClient ABC — abstracts LLM provider behind a uniform chat interface."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod


class BaseModelClient(ABC):
    """
    Uniform async chat-completion interface.

    Contract rules:
    - chat_completion() MUST be async.
    - chat_completion() MUST raise LLMProviderError (from recall.errors) on failure.
    - from_env() MUST read RECALL_LLM_PROVIDER and return the matching subclass.
    - Subclasses MUST NOT import from storage, retrieval, or capture layers.
    """

    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict],
        model: str,
        **kwargs,
    ) -> str:
        """
        Call the LLM and return the assistant reply as a plain string.

        Args:
            messages: OpenAI-format message list [{"role": ..., "content": ...}, ...]
            model: Model identifier string (provider-specific).
            **kwargs: Extra params forwarded to the provider (temperature, max_tokens, etc.)

        Returns:
            The assistant reply as a string.

        Raises:
            LLMProviderError: On any provider-side failure.
        """

    @classmethod
    def from_env(cls) -> "BaseModelClient":
        """
        Factory: reads RECALL_LLM_PROVIDER env var, returns the matching subclass.

        Supported values: openrouter | ollama | vllm | claude | lmstudio
        Defaults to openrouter.
        """
        from recall.model.base import get_client_for_provider  # noqa: PLC0415

        provider = os.getenv("RECALL_LLM_PROVIDER", "openrouter").strip().lower()
        return get_client_for_provider(provider)
