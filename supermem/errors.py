"""Typed exception hierarchy for supermem.

Every exception is a subclass of SupermemError and carries a recovery_hint
string that is safe to surface to users and log at WARNING level.
"""

from __future__ import annotations


class SupermemError(Exception):
    """
    Base class for all supermem exceptions.

    Attributes:
        recovery_hint: A human-readable suggestion for how to recover.
    """

    recovery_hint: str = "Check the supermem logs for details."

    def __init__(self, message: str, recovery_hint: str | None = None):
        super().__init__(message)
        if recovery_hint is not None:
            self.recovery_hint = recovery_hint


# Backward-compatible alias for older imports.
RecallError = SupermemError


class StorageError(SupermemError):
    """Raised when a read/write to SQLite, Kuzu, or Chroma fails."""

    recovery_hint = (
        "Check that the database path is writable and not locked by another process. "
        "Run `supermem serve` fresh or inspect SUPERMEM_DB_PATH."
    )


class VaultIndexError(SupermemError):
    """Raised when the VaultIndexer cannot read or parse a markdown file."""

    recovery_hint = (
        "Ensure the vault path exists and contains valid UTF-8 markdown files. "
        "Run `supermem serve` to trigger a full re-index."
    )


class GraphTraversalError(SupermemError):
    """Raised when Kuzu graph traversal fails or returns corrupt data."""

    recovery_hint = (
        "The graph database may be corrupt. "
        "Run `POST /index/rebuild` via the worker API to rebuild the entity graph."
    )


class SandboxTimeoutError(SupermemError):
    """Raised when the agent's sandboxed code execution exceeds its time limit."""

    recovery_hint = (
        "The agent's code block exceeded the sandbox timeout (default 20 s). "
        "Try a simpler query or increase SUPERMEM_SANDBOX_TIMEOUT."
    )


class FilePermissionError(SupermemError):
    """Raised when the sandbox tries to access a path outside the vault."""

    recovery_hint = (
        "The agent attempted to access a file outside the memory vault. "
        "This is a security boundary — the access was blocked."
    )


class LLMProviderError(SupermemError):
    """Raised when an LLM provider call fails (network, auth, rate-limit, etc.)."""

    recovery_hint = (
        "Check your API key and network connectivity. "
        "Verify SUPERMEM_LLM_PROVIDER is set correctly and the provider is reachable."
    )


class ProviderNotConfiguredError(LLMProviderError):
    """Raised when a required env var for the chosen provider is missing."""

    recovery_hint = (
        "A required environment variable for the selected LLM provider is missing. "
        "Check .env against .env.example and ensure all required keys are set."
    )


class AuthError(SupermemError):
    """Raised when a Bearer token is required but missing or invalid."""

    recovery_hint = (
        "Set SUPERMEM_API_KEY in .env and include it as a Bearer token in requests: "
        "'Authorization: Bearer <your-key>'."
    )


class RateLimitError(SupermemError):
    """Raised when a client exceeds the configured request rate."""

    recovery_hint = (
        "Too many requests. Wait a moment and retry, or raise SUPERMEM_RATE_LIMIT."
    )
