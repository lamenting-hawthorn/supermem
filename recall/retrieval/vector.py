"""VectorRetriever — Tier 3: ChromaDB semantic search (optional)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from recall.core.retriever import BaseRetriever, RetrievalResult
from recall.logging import get_logger

if TYPE_CHECKING:
    from recall.storage.vector import ChromaManager

log = get_logger(__name__)


class VectorRetriever(BaseRetriever):
    """
    Tier 3 — ChromaDB semantic search.

    Optional. Disabled by default (RECALL_VECTOR=false).
    Finds semantically related content that keyword search would miss.
    """

    def __init__(self, chroma: "ChromaManager | None" = None) -> None:
        self._chroma = chroma

    @property
    def tier(self) -> int:
        return 3

    @property
    def available(self) -> bool:
        return self._chroma is not None and self._chroma.available

    async def search(self, query: str, limit: int = 10) -> RetrievalResult:
        t0 = self._now_ms()
        if not self.available or self._chroma is None:
            return RetrievalResult(source_tier=self.tier, latency_ms=self._now_ms() - t0)
        try:
            obs_ids = await self._chroma.search(query, limit=limit)
            latency = self._now_ms() - t0
            log.debug("vector_search", query=query, found=len(obs_ids), latency_ms=round(latency, 2))
            return RetrievalResult(
                obs_ids=obs_ids,
                source_tier=self.tier,
                latency_ms=latency,
            )
        except Exception as exc:
            log.warning("vector_retriever_failed", error=str(exc))
            return RetrievalResult(source_tier=self.tier, latency_ms=self._now_ms() - t0)
