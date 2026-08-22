"""Unit tests for HybridRetriever — tiered orchestration and graceful degradation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from supermem.core.retriever import RetrievalResult
from supermem.retrieval.hybrid import HybridRetriever
from supermem.storage.database import DatabaseManager
from supermem.storage.graph import KuzuGraphManager
from supermem.storage.vector import ChromaManager


class _FakeRanked:
    """Stand-in for a content-tier retriever returning a fixed ranking."""

    def __init__(self, tier: int, ranked: list[int]):
        self._tier = tier
        self._ranked = ranked

    @property
    def tier(self) -> int:
        return self._tier

    @property
    def available(self) -> bool:
        return True

    async def search(self, query: str, limit: int = 10) -> RetrievalResult:
        await asyncio.sleep(0)  # yield so concurrent gather actually interleaves
        return RetrievalResult(obs_ids=self._ranked[:limit], source_tier=self._tier)


class _FakeGraph(_FakeRanked):
    """Stand-in for GraphRetriever.expand_from (seed-based enrichment)."""

    async def expand_from(
        self,
        seed_obs_ids: list[int],
        exclude_ids: set[int] | None = None,
        hops: int = 2,
        limit: int = 20,
    ) -> RetrievalResult:
        await asyncio.sleep(0)
        exclude = exclude_ids or set()
        new_ids = [i for i in self._ranked if i not in exclude][:limit]
        return RetrievalResult(obs_ids=new_ids, source_tier=self._tier)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "hybrid_test.db")
    await d.init()
    yield d
    await d.close()


@pytest.fixture
def graph(tmp_path: Path) -> KuzuGraphManager:
    g = KuzuGraphManager(tmp_path / "graph")
    g.init()
    return g


@pytest.fixture
def chroma() -> ChromaManager:
    # SUPERMEM_VECTOR is false by default → unavailable
    return ChromaManager()


@pytest_asyncio.fixture
async def retriever(
    db: DatabaseManager, graph: KuzuGraphManager, chroma: ChromaManager
) -> HybridRetriever:
    return HybridRetriever(db=db, graph=graph, chroma=chroma)


@pytest.mark.asyncio
async def test_tier1_finds_keyword_match(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    await db.write_observation("alice works at acme corporation")
    result = await retriever.search("alice", tier_limit=1)
    assert result.source_tier == 1
    assert len(result.obs_ids) >= 1


@pytest.mark.asyncio
async def test_empty_query_returns_empty(retriever: HybridRetriever) -> None:
    result = await retriever.search("zzznomatchxxx", tier_limit=1)
    assert result.obs_ids == []


@pytest.mark.asyncio
async def test_tier3_skipped_when_unavailable(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    await db.write_observation("vector search test")
    # With SUPERMEM_VECTOR=false, tier 3 is skipped — no error raised
    result = await retriever.search("vector search test", tier_limit=3)
    assert result.source_tier <= 2  # only tiers 1-2 ran


@pytest.mark.asyncio
async def test_get_observations(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    oid = await db.write_observation("full content fetch")
    obs = await retriever.get_observations([oid])
    assert len(obs) == 1
    assert obs[0]["content"] == "full content fetch"


@pytest.mark.asyncio
async def test_get_timeline(db: DatabaseManager, retriever: HybridRetriever) -> None:
    sid = await db.create_session()
    ids = []
    for i in range(5):
        oid = await db.write_observation(f"timeline obs {i}", session_id=sid)
        ids.append(oid)
    timeline = await retriever.get_timeline(ids[2], window=2)
    assert len(timeline) > 0


@pytest.mark.asyncio
async def test_latency_recorded(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    await db.write_observation("latency test content")
    result = await retriever.search("latency test", tier_limit=1)
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_dedup_across_tiers(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    await db.write_observation("dedup test alice")
    result = await retriever.search("dedup test alice", tier_limit=2)
    # IDs should be deduplicated — no duplicates in result
    assert len(result.obs_ids) == len(set(result.obs_ids))


@pytest.mark.asyncio
async def test_retracted_observations_filtered_from_search(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    oid = await db.write_observation("Project codename is Bluebird")
    assert oid in (await retriever.search("Bluebird", tier_limit=1)).obs_ids

    await db.retract_observation(oid, reason="superseded")

    result = await retriever.search("Bluebird", tier_limit=1)
    assert oid not in result.obs_ids


@pytest.mark.asyncio
async def test_tier_four_request_is_capped_without_agent_fallback(
    retriever: HybridRetriever,
) -> None:
    result = await retriever.search("no-tier-four-canary", tier_limit=4)

    assert not hasattr(retriever, "_agent")
    assert result.obs_ids == []
    assert result.source_tier != 4


# ── RRF fusion over fake ranked tiers ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_rrf_fuses_two_lists_with_hand_computed_order(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    ids = {await db.write_observation(f"rrf fusion doc {i}"): i for i in range(4)}
    a, b, c, d = ids  # insertion order == id order
    retriever._fts = _FakeRanked(1, [a, b, c])
    retriever._vector = _FakeRanked(3, [c, d])

    result = await retriever.search("rrf fusion", tier_limit=3)

    # Hand-computed RRF (k=60): c = 1/62 + 1/61 ≈ 0.03252 wins;
    # a = 1/61 second; b and d tie at 1/62 → first-seen (b) before d.
    assert result.obs_ids == [c, a, b, d]
    assert result.source_tier == 3
    assert result.metadata["fusion"] == "rrf"
    assert result.metadata["tiers"] == ["fts", "vector"]


@pytest.mark.asyncio
async def test_consensus_doc_outranks_single_list_leader(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    x = await db.write_observation("consensus doc mid both lists")
    y = await db.write_observation("solo leader only in fts list")
    z = await db.write_observation("tail filler one")
    w = await db.write_observation("tail filler two")
    retriever._fts = _FakeRanked(1, [y, x, z])
    retriever._vector = _FakeRanked(3, [w, x])

    result = await retriever.search("consensus", tier_limit=3)

    # x is mid-list in BOTH lists: 1/61 + 1/62 beats y's solo rank-1 1/61.
    assert result.obs_ids[0] == x


@pytest.mark.asyncio
async def test_lifecycle_filter_applied_after_fusion(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    retracted_id = await db.write_observation("retractable rrf candidate")
    active_id = await db.write_observation("surviving rrf candidate")
    await db.retract_observation(retracted_id, reason="superseded")
    retriever._fts = _FakeRanked(1, [retracted_id, active_id])

    result = await retriever.search("rrf candidate", tier_limit=1)

    # Retracted doc ranked FIRST by FTS must still never surface.
    assert result.obs_ids == [active_id]


@pytest.mark.asyncio
async def test_vector_unavailable_preserves_pure_fts_ordering(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    first = await db.write_observation("fts ordering top hit")
    second = await db.write_observation("fts ordering runner up")
    third = await db.write_observation("fts ordering tail")
    retriever._fts = _FakeRanked(1, [third, first, second])
    # Default chroma fixture is unavailable → vector contributes nothing.

    result = await retriever.search("ordering", tier_limit=3)

    assert result.obs_ids == [third, first, second]
    assert result.metadata["tiers"] == ["fts"]
    assert result.source_tier == 1


@pytest.mark.asyncio
async def test_all_tiers_empty_returns_empty(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    retriever._fts = _FakeRanked(1, [])
    retriever._vector = _FakeRanked(3, [])

    result = await retriever.search("nothing anywhere", tier_limit=3)

    assert result.obs_ids == []
    assert result.metadata["tiers"] == []


@pytest.mark.asyncio
async def test_vector_rescues_paraphrase_missed_by_fts(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    """LongMemEval gap: semantic match with zero keyword overlap must surface."""
    para_id = await db.write_observation(
        "I am thinking about the Sony A7IV camera for video."
    )
    retriever._fts = _FakeRanked(1, [])  # keyword search misses entirely
    retriever._vector = _FakeRanked(3, [para_id])  # semantic match ranks first

    result = await retriever.search(
        "what camera does the user want to buy?", tier_limit=3
    )

    assert result.obs_ids == [para_id]
    assert result.metadata["tiers"] == ["vector"]


@pytest.mark.asyncio
async def test_graph_enrichment_appends_behind_fused_ordering(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    a = await db.write_observation("fused seed observation alpha")
    b = await db.write_observation("fused seed observation beta")
    related = await db.write_observation("graph-expanded neighbour gamma")
    retriever._fts = _FakeRanked(1, [b, a])
    retriever._graph_retriever = _FakeGraph(2, [related, b])

    result = await retriever.search("seed observations", tier_limit=2)

    # Graph neighbours append after the fused ranking; already-fused ids dedup.
    assert result.obs_ids == [b, a, related]
    assert result.metadata["tiers"] == ["fts", "graph"]
    assert result.source_tier == 2
