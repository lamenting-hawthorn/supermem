"""HybridRetriever — orchestrates all four retrieval tiers.

Tier 1 (FTS5) → Tier 2 (Kuzu graph) → Tier 3 (ChromaDB) → Tier 4 (Agent).

Short-circuits when min_results is reached. Skips unavailable tiers with
a WARNING log entry (graceful degradation). Returns merged, deduplicated
RetrievalResult with source_tier metadata for each obs_id.

Apache 2.0 — original implementation.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from recall.core.retriever import BaseRetriever, RetrievalResult
from recall.logging import get_logger

if TYPE_CHECKING:
    from recall.storage.database import DatabaseManager
    from recall.storage.graph import KuzuGraphManager
    from recall.storage.vector import ChromaManager

log = get_logger(__name__)


class HybridRetriever:
    """
    Orchestrates the four retrieval tiers in order.

    Usage:
        retriever = HybridRetriever(db=db, graph=graph, chroma=chroma)
        result = await retriever.search("who is alice?", tier_limit=3)
        obs = await retriever.get_observations(result.obs_ids)
    """

    def __init__(
        self,
        db: "DatabaseManager",
        graph: "KuzuGraphManager",
        chroma: "ChromaManager | None" = None,
        memory_path: str | None = None,
    ) -> None:
        from recall.retrieval.fts import FTSRetriever
        from recall.retrieval.graph import GraphRetriever
        from recall.retrieval.vector import VectorRetriever
        from recall.retrieval.agent import AgentRetriever

        self._db = db
        self._fts = FTSRetriever(db)
        self._graph_retriever = GraphRetriever(db, graph)
        self._vector = VectorRetriever(chroma)
        self._agent = AgentRetriever(memory_path=memory_path, db=db)

    # ── Main entry point ──────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        tier_limit: int = 4,
        min_results: int = 3,
        limit: int = 20,
    ) -> RetrievalResult:
        """
        Search through tiers 1 → tier_limit in order.

        - Short-circuits when len(obs_ids) >= min_results.
        - Skips unavailable tiers (logs WARNING).
        - Returns merged, deduplicated obs_ids with source_tier of the
          last tier that contributed results.

        Args:
            query: Natural language query.
            tier_limit: Maximum tier to try (1–4). Default 4.
            min_results: Stop early when this many results are found.
            limit: Max total obs_ids to return.

        Returns:
            RetrievalResult with merged obs_ids and source_tier of highest tier used.
        """
        t0 = time.monotonic() * 1000
        all_ids: list[int] = []
        highest_tier = 0
        tier1_ids: list[int] = []

        # ── Tier 1: FTS5 ─────────────────────────────────────────────────────
        if tier_limit >= 1:
            r1 = await self._fts.search(query, limit=limit)
            tier1_ids = r1.obs_ids
            all_ids = _merge(all_ids, tier1_ids)
            if r1.obs_ids:
                highest_tier = 1
            if len(all_ids) >= min_results and tier_limit == 1:
                return self._build(all_ids[:limit], highest_tier, t0)

        # ── Tier 2: Kuzu graph ────────────────────────────────────────────────
        if tier_limit >= 2:
            if not self._graph_retriever.available:
                log.warning("tier2_unavailable", reason="Kuzu not initialised")
            else:
                r2 = await self._graph_retriever.expand_from(
                    seed_obs_ids=tier1_ids,
                    exclude_ids=set(all_ids),
                    limit=limit,
                )
                all_ids = _merge(all_ids, r2.obs_ids)
                if r2.obs_ids:
                    highest_tier = 2

            if len(all_ids) >= min_results:
                return self._build(all_ids[:limit], highest_tier, t0)

        # ── Tier 3: ChromaDB (optional) ───────────────────────────────────────
        if tier_limit >= 3:
            if not self._vector.available:
                log.debug("tier3_skipped", reason="RECALL_VECTOR=false or chromadb unavailable")
            else:
                r3 = await self._vector.search(query, limit=limit)
                new_ids = [i for i in r3.obs_ids if i not in set(all_ids)]
                all_ids = _merge(all_ids, new_ids)
                if new_ids:
                    highest_tier = 3

            if len(all_ids) >= min_results:
                return self._build(all_ids[:limit], highest_tier, t0)

        # ── Tier 4: Agent fallback ────────────────────────────────────────────
        if tier_limit >= 4:
            log.info("tier4_agent_fallback", query=query, prior_results=len(all_ids))
            r4 = await self._agent.search(query, limit=1)
            new_ids = [i for i in r4.obs_ids if i not in set(all_ids)]
            all_ids = _merge(all_ids, new_ids)
            if new_ids:
                highest_tier = 4

        return self._build(all_ids[:limit], highest_tier, t0)

    # ── Convenience pass-throughs ─────────────────────────────────────────────

    async def get_observations(self, ids: list[int]) -> list[dict]:
        """Batch fetch full observation records by IDs."""
        return await self._db.get_observations(ids)

    async def get_timeline(self, obs_id: int, window: int = 5) -> list[dict]:
        """Chronological context around an observation."""
        return await self._db.get_timeline(obs_id, window)

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build(obs_ids: list[int], tier: int, t0: float) -> RetrievalResult:
        latency = time.monotonic() * 1000 - t0
        log.info(
            "hybrid_search_done",
            results=len(obs_ids),
            highest_tier=tier,
            latency_ms=round(latency, 1),
        )
        return RetrievalResult(obs_ids=obs_ids, source_tier=tier, latency_ms=latency)


def _merge(existing: list[int], new: list[int]) -> list[int]:
    """Append new IDs not already in existing, preserving order."""
    seen = set(existing)
    return existing + [i for i in new if i not in seen]
