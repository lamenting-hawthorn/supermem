"""BaseStorage ABC — implemented by DatabaseManager, KuzuGraphManager, ChromaManager."""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseStorage(ABC):
    """
    Abstract base for all storage backends.

    Contract rules:
    - write() returns the new record's integer ID.
    - read() returns None when ID is not found (never raises).
    - delete() returns True on success, False if ID was not found.
    - health() returns True when the backend is reachable/writable.
    - Implementations MUST NOT import from retrieval or capture layers.
    """

    @abstractmethod
    async def write(self, record: dict) -> int:
        """Persist record. Returns new row ID."""

    @abstractmethod
    async def read(self, id: int) -> dict | None:
        """Fetch record by ID. Returns None if missing."""

    @abstractmethod
    async def delete(self, id: int) -> bool:
        """Remove record. Returns True if deleted, False if not found."""

    @abstractmethod
    async def health(self) -> bool:
        """Liveness check — True means the backend is usable."""
