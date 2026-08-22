"""HybridRetriever — RRF-fused lifecycle-aware retrieval over tiers 1–3.

Content tiers (FTS5, ChromaDB vector) run concurrently and their ranked
id-lists are fused with Reciprocal Rank Fusion (RRF). Lifecycle/authority
filtering is applied AFTER fusion so retracted/superseded/archived/expired/
out-of-validity rows can never surface regardless of where a tier ranked
them. The Kuzu graph tier is not a query-ranked signal — it expands entity
neighbours from seeds — so it stays as a post-fusion enrichment step that
appends related ids behind the fused ranking.

Tier 4 (raw-vault Agent) remains outside the fusion ladder: requests are
capped at tier 3 and no agent fallback is invoked.

Apache 2.0 — original implementation.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from supermem.config import SUPERMEM_MAX_RETRIEVAL_TIER
from supermem.core.retriever import RetrievalResult
from supermem.logging import get_logger

if TYPE_CHECKING:
    from supermem.storage.database import DatabaseManager
    from supermem.storage.graph import KuzuGraphManager
    from supermem.storage.vector import ChromaManager

log = get_logger(__name__)

RRF_K = 60
"""Reciprocal Rank Fusion smoothing constant (standard k=60)."""

_TIER_NAMES = {1: "fts", 2: "graph", 3: "vector"}
_TIER_NUMBERS = {name: num for num, name in _TIER_NAMES.items()}


def rrf_fuse(*ranked_lists: list[int]) -> list[int]:
    """Fuse ranked id-lists with Reciprocal Rank Fusion.

    score(d) = Σ 1/(RRF_K + rankᵢ(d)) over every list containing d,
    where rank starts at 1. Results are ordered by fused score descending;
    ties break deterministically by first-seen order across the lists
    in call order.
    """
    scores: dict[int, float] = {}
    first_seen: dict[int, int] = {}
    for ranked in ranked_lists:
        for rank, obs_id in enumerate(ranked, start=1):
            if obs_id not in first_seen:
                first_seen[obs_id] = len(first_seen)
            scores[obs_id] = scores.get(obs_id, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda oid: (-scores[oid], first_seen[oid]))


class HybridRetriever:
    """
    Fuses the supported lifecycle-aware retrieval tiers with RRF.

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
        from supermem.retrieval.fts import FTSRetriever
        from supermem.retrieval.graph import GraphRetriever
        from supermem.retrieval.vector import VectorRetriever

        self._db = db
        self._fts = FTSRetriever(db)
        self._graph_retriever = GraphRetriever(db, graph)
        self._vector = VectorRetriever(chroma)
        del memory_path

    # ── Main entry point ──────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        tier_limit: int = SUPERMEM_MAX_RETRIEVAL_TIER,
        min_results: int = 3,
        limit: int = 20,
    ) -> RetrievalResult:
        """
        Search through supported tiers 1 → 3 with RRF fusion.

        - FTS and vector run concurrently; ranked lists are fused with
          Reciprocal Rank Fusion (k=60).
        - Lifecycle/authority filtering runs AFTER fusion, so inactive
          observations can never surface no matter how a tier ranked them.
        - Graph expansion enriches fused results post-fusion (entity
          neighbours appended behind the fused ranking).
        - Skips unavailable tiers (logs WARNING); graceful degradation.
        - Returns obs_ids ordered by fused score with source_tier of the
          highest contributing content tier and metadata["tiers"] listing
          every tier that contributed results.

        Args:
            query: Natural language query.
            tier_limit: Maximum tier to try (1–3). Values above 3 are capped;
              tier 4 is never invoked.
            min_results: Retained for API compatibility; all available tiers
              always run concurrently, so no short-circuiting occurs.
            limit: Max total obs_ids to return (always respected).

        Returns:
            RetrievalResult with fused obs_ids, source_tier of the highest
            contributing tier, and metadata["tiers"] attribution.
        """
        tier_limit = min(tier_limit, SUPERMEM_MAX_RETRIEVAL_TIER)
        t0 = time.monotonic() * 1000

        # ── Concurrent content tiers: FTS5 (+ Vector when available) ─────────
        tasks = [self._fts.search(query, limit=limit)]
        if tier_limit >= 3 and self._vector.available:
            tasks.append(self._vector.search(query, limit=limit))
        else:
            log.warning("tier3_skipped", reason="unavailable or above tier_limit")
        r1, *rest = await asyncio.gather(*tasks)
        r3 = rest[0] if rest else None

        contributing: list[str] = []
        if r1.obs_ids:
            contributing.append(_TIER_NAMES[r1.source_tier])
        if r3 is not None and r3.obs_ids:
            contributing.append(_TIER_NAMES[r3.source_tier])

        fused_ids = rrf_fuse(r1.obs_ids, r3.obs_ids if r3 else [])

        # ── Lifecycle authority filter AFTER fusion ──────────────────────────
        fused_ids = await self._active_ids(fused_ids)

        # ── Graph enrichment (Tier 2): entity expansion from fused seeds ─────
        # The graph tier does not rank by query relevance — it BFS-expands
        # entities mentioned in seed observations — so it appends new ids
        # behind the fused ordering rather than participating in RRF.
        if tier_limit >= 2 and self._graph_retriever.available:
            r2 = await self._graph_retriever.expand_from(
                seed_obs_ids=fused_ids,
                exclude_ids=set(fused_ids),
                limit=limit,
            )
            if r2.obs_ids:
                contributing.append(_TIER_NAMES[r2.source_tier])
                enriched = await self._active_ids(r2.obs_ids)
                seen = set(fused_ids)
                fused_ids += [i for i in enriched if i not in seen]

        highest_tier = max((_TIER_NUMBERS[name] for name in contributing), default=0)
        return self._build(fused_ids[:limit], highest_tier, t0, tiers=contributing)

    # ── Convenience pass-throughs ─────────────────────────────────────────────

    async def get_observations(self, ids: list[int]) -> list[dict]:
        """Batch fetch full observation records by IDs."""
        return await self._db.get_observations(ids)

    async def _active_ids(self, ids: list[int]) -> list[int]:
        """Filter candidate ids through the database lifecycle status."""
        return await self._db.active_obs_ids(ids)

    async def get_timeline(self, obs_id: int, window: int = 5) -> list[dict]:
        """Chronological context around an observation."""
        return await self._db.get_timeline(obs_id, window)

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build(
        obs_ids: list[int], tier: int, t0: float, tiers: list[str]
    ) -> RetrievalResult:
        latency = time.monotonic() * 1000 - t0
        log.info(
            "hybrid_search_done",
            results=len(obs_ids),
            highest_tier=tier,
            tiers=tiers,
            latency_ms=round(latency, 1),
        )
        return RetrievalResult(
            obs_ids=obs_ids,
            source_tier=tier,
            latency_ms=latency,
            metadata={"fusion": "rrf", "tiers": tiers},
        )
