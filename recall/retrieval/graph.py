"""GraphRetriever — Tier 2: Kuzu graph expansion of FTS hits."""
from __future__ import annotations

from typing import TYPE_CHECKING

from recall.core.retriever import BaseRetriever, RetrievalResult
from recall.logging import get_logger

if TYPE_CHECKING:
    from recall.storage.database import DatabaseManager
    from recall.storage.graph import KuzuGraphManager

log = get_logger(__name__)


class GraphRetriever(BaseRetriever):
    """
    Tier 2 — Kuzu graph expansion.

    Takes obs_ids from tier 1, seeds the entity graph with their mentions,
    expands via BFS to related entities, then resolves back to obs_ids.
    Returns NEW obs_ids not already found by FTS.
    """

    def __init__(self, db: "DatabaseManager", graph: "KuzuGraphManager") -> None:
        self._db = db
        self._graph = graph

    @property
    def tier(self) -> int:
        return 2

    @property
    def available(self) -> bool:
        return self._graph.available

    async def search(self, query: str, limit: int = 10) -> RetrievalResult:
        """
        Graph expansion is not a standalone search — it is seeded by tier 1 hits.
        When called standalone (e.g. in tests), falls back to empty result.
        Use expand_from() to provide seed obs_ids.
        """
        return RetrievalResult(source_tier=self.tier)

    async def expand_from(
        self,
        seed_obs_ids: list[int],
        exclude_ids: set[int] | None = None,
        hops: int = 2,
        limit: int = 20,
    ) -> RetrievalResult:
        """
        Expand from seed obs_ids via the entity graph.

        1. Look up entity names that appear in the seed observations.
        2. BFS-expand those entities through LINKS_TO edges.
        3. Resolve expanded entity names back to observation IDs.
        """
        t0 = self._now_ms()
        if not self.available or not seed_obs_ids:
            return RetrievalResult(source_tier=self.tier, latency_ms=self._now_ms() - t0)

        try:
            entity_names = await self._db.entities_for_obs_ids(seed_obs_ids)
            if not entity_names:
                return RetrievalResult(source_tier=self.tier, latency_ms=self._now_ms() - t0)

            related = self._graph.expand(entity_names, hops=hops)
            if not related:
                return RetrievalResult(source_tier=self.tier, latency_ms=self._now_ms() - t0)

            new_ids = await self._db.obs_ids_for_entities(related)
            exclude = exclude_ids or set()
            new_ids = [i for i in new_ids if i not in exclude][:limit]

            latency = self._now_ms() - t0
            log.debug(
                "graph_expand",
                seeds=len(seed_obs_ids),
                entities=len(entity_names),
                related=len(related),
                new_obs=len(new_ids),
                latency_ms=round(latency, 2),
            )
            return RetrievalResult(
                obs_ids=new_ids,
                source_tier=self.tier,
                latency_ms=latency,
                metadata={"entity_seeds": entity_names, "related_entities": related},
            )
        except Exception as exc:
            log.warning("graph_retriever_failed", error=str(exc))
            return RetrievalResult(source_tier=self.tier, latency_ms=self._now_ms() - t0)
