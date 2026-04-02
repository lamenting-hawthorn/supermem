"""BaseRetriever ABC — implemented by each of the four retrieval tiers."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RetrievalResult:
    """Unified result returned by every retrieval tier."""

    obs_ids: list[int] = field(default_factory=list)
    """Observation IDs found by this tier."""

    source_tier: int = 0
    """Which tier answered: 1=FTS5, 2=Kuzu graph, 3=ChromaDB, 4=Agent."""

    latency_ms: float = 0.0
    """Wall-clock time for this tier's search, in milliseconds."""

    metadata: dict = field(default_factory=dict)
    """Tier-specific extras (e.g. BFS depth, similarity scores)."""

    @property
    def found(self) -> bool:
        return bool(self.obs_ids)


class BaseRetriever(ABC):
    """
    Abstract base for all retrieval tiers.

    Contract rules (enforced by Architect):
    - Implementations MUST NOT import directly from other retrieval tiers.
    - Implementations MUST check self.available before doing real work.
    - Implementations MUST record latency_ms on every returned RetrievalResult.
    - Implementations MUST NOT raise on failure — return empty RetrievalResult instead.
    """

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> RetrievalResult:
        """Execute search and return results. Never raises — degrades gracefully."""

    @property
    @abstractmethod
    def tier(self) -> int:
        """Tier number: 1–4."""

    @property
    @abstractmethod
    def available(self) -> bool:
        """True if this tier's backend is reachable. Used for graceful degradation."""

    @staticmethod
    def _now_ms() -> float:
        return time.monotonic() * 1000
