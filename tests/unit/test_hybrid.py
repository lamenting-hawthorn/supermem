"""Unit tests for HybridRetriever — tiered orchestration and graceful degradation."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from recall.retrieval.hybrid import HybridRetriever
from recall.storage.database import DatabaseManager
from recall.storage.graph import KuzuGraphManager
from recall.storage.vector import ChromaManager


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
    # RECALL_VECTOR is false by default → unavailable
    return ChromaManager()


@pytest_asyncio.fixture
async def retriever(db: DatabaseManager, graph: KuzuGraphManager, chroma: ChromaManager) -> HybridRetriever:
    return HybridRetriever(db=db, graph=graph, chroma=chroma)


@pytest.mark.asyncio
async def test_tier1_finds_keyword_match(db: DatabaseManager, retriever: HybridRetriever) -> None:
    await db.write_observation("alice works at acme corporation")
    result = await retriever.search("alice", tier_limit=1)
    assert result.source_tier == 1
    assert len(result.obs_ids) >= 1


@pytest.mark.asyncio
async def test_empty_query_returns_empty(retriever: HybridRetriever) -> None:
    result = await retriever.search("zzznomatchxxx", tier_limit=1)
    assert result.obs_ids == []


@pytest.mark.asyncio
async def test_tier3_skipped_when_unavailable(db: DatabaseManager, retriever: HybridRetriever) -> None:
    await db.write_observation("vector search test")
    # With RECALL_VECTOR=false, tier 3 is skipped — no error raised
    result = await retriever.search("vector search test", tier_limit=3)
    assert result.source_tier <= 2  # only tiers 1-2 ran


@pytest.mark.asyncio
async def test_get_observations(db: DatabaseManager, retriever: HybridRetriever) -> None:
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
async def test_latency_recorded(db: DatabaseManager, retriever: HybridRetriever) -> None:
    await db.write_observation("latency test content")
    result = await retriever.search("latency test", tier_limit=1)
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_dedup_across_tiers(db: DatabaseManager, retriever: HybridRetriever) -> None:
    await db.write_observation("dedup test alice")
    result = await retriever.search("dedup test alice", tier_limit=2)
    # IDs should be deduplicated — no duplicates in result
    assert len(result.obs_ids) == len(set(result.obs_ids))
