"""Integration tests — real temp vault → index → search all tiers.

These tests spin up real storage (SQLite, optional Kuzu), write real markdown
files to a temp vault, run the VaultIndexer, and assert that the HybridRetriever
finds the expected observations without mocking any storage layer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from recall.capture.observation import ObservationCapture
from recall.capture.session import SessionManager
from recall.indexer.vault import VaultIndexer
from recall.retrieval.hybrid import HybridRetriever
from recall.storage.database import DatabaseManager
from recall.storage.graph import KuzuGraphManager
from recall.storage.vector import ChromaManager


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "integration.db")
    await d.init()
    yield d
    await d.close()


@pytest.fixture
def graph(tmp_path: Path) -> KuzuGraphManager:
    g = KuzuGraphManager(tmp_path / "graph")
    g.init()
    return g


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest_asyncio.fixture
async def retriever(db: DatabaseManager, graph: KuzuGraphManager) -> HybridRetriever:
    chroma = ChromaManager()
    return HybridRetriever(db=db, graph=graph, chroma=chroma)


# ---------------------------------------------------------------------------
# Full pipeline: write markdown → index → search via tier 1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vault_index_then_fts_search(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path, retriever: HybridRetriever
) -> None:
    """Index a markdown file and confirm tier-1 FTS finds it."""
    (vault / "alice.md").write_text("Alice works at Acme Corporation.\nShe leads the product team.")
    indexer = VaultIndexer(db=db, graph=graph, vault_path=vault)
    await indexer.walk()

    result = await retriever.search("Alice Acme", tier_limit=1)
    assert result.source_tier == 1
    assert len(result.obs_ids) >= 1


@pytest.mark.asyncio
async def test_multiple_files_all_indexed(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path, retriever: HybridRetriever
) -> None:
    """All markdown files in vault are indexed; each is searchable."""
    (vault / "alice.md").write_text("Alice is a software engineer.")
    (vault / "bob.md").write_text("Bob manages the data pipeline.")
    (vault / "carol.md").write_text("Carol runs the design team.")
    indexer = VaultIndexer(db=db, graph=graph, vault_path=vault)
    await indexer.walk()

    for name in ("Alice", "Bob", "Carol"):
        result = await retriever.search(name, tier_limit=1)
        assert len(result.obs_ids) >= 1, f"Expected to find {name}"


@pytest.mark.asyncio
async def test_private_content_not_indexed(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path, retriever: HybridRetriever
) -> None:
    """Content inside <private>...</private> must NOT appear in FTS results."""
    (vault / "secrets.md").write_text(
        "Public intro.\n<private>API_KEY=supersecret_12345</private>\nPublic outro."
    )
    indexer = VaultIndexer(db=db, graph=graph, vault_path=vault)
    await indexer.walk()

    result = await retriever.search("supersecret_12345", tier_limit=1)
    assert result.obs_ids == []

    # But the public content IS indexed
    result_pub = await retriever.search("Public intro", tier_limit=1)
    assert len(result_pub.obs_ids) >= 1


@pytest.mark.asyncio
async def test_wikilinks_create_graph_edges(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path
) -> None:
    """Wikilinks in markdown create edges in the Kuzu graph."""
    if not graph.available:
        pytest.skip("kuzu not installed")

    (vault / "alice.md").write_text("Alice works with [[Bob]] and [[Carol]].")
    (vault / "bob.md").write_text("Bob is a data engineer.")
    (vault / "carol.md").write_text("Carol is a designer.")
    indexer = VaultIndexer(db=db, graph=graph, vault_path=vault)
    await indexer.walk()

    # alice should have edges to bob and carol
    related = graph.expand(["alice"], hops=1)
    names_lower = [r.lower() for r in related]
    assert "bob" in names_lower or "Bob" in related
    assert "carol" in names_lower or "Carol" in related


@pytest.mark.asyncio
async def test_tier2_graph_expansion(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path, retriever: HybridRetriever
) -> None:
    """Tier 2 graph expansion finds observations reachable via wikilinks."""
    if not graph.available:
        pytest.skip("kuzu not installed")

    (vault / "alice.md").write_text("Alice is the CTO. She works with [[Bob]].")
    (vault / "bob.md").write_text("Bob manages a distributed caching system.")
    indexer = VaultIndexer(db=db, graph=graph, vault_path=vault)
    await indexer.walk()

    # Query for "alice" — tier 1 finds alice.md obs, tier 2 should expand to bob.md
    result = await retriever.search("alice cto", tier_limit=2)
    assert result.source_tier <= 2
    assert len(result.obs_ids) >= 1


@pytest.mark.asyncio
async def test_observation_capture_then_search(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    """Observations written via ObservationCapture are searchable immediately."""
    cap = ObservationCapture(db)
    await cap.record("The quarterly revenue target is 2.5 million", tier_used=1, latency_ms=5.0)

    result = await retriever.search("quarterly revenue", tier_limit=1)
    assert len(result.obs_ids) >= 1


@pytest.mark.asyncio
async def test_session_lifecycle(db: DatabaseManager) -> None:
    """Full session start → write observations → end flow doesn't raise."""
    mgr = SessionManager(db)
    cap = ObservationCapture(db)

    sid = await mgr.start()
    for i in range(5):
        await cap.record(f"integration test observation {i}", session_id=sid)
    # End without model_client → no LLM call, just closes session
    await mgr.end(sid, model_client=None)

    stats = await db.get_stats()
    assert stats["obs_count"] >= 5
    assert stats["session_count"] >= 1


@pytest.mark.asyncio
async def test_dedup_prevents_duplicate_indexing(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path, retriever: HybridRetriever
) -> None:
    """Re-indexing the same file does not create duplicate observations."""
    md_file = vault / "stable.md"
    md_file.write_text("Stable content that never changes.")
    indexer = VaultIndexer(db=db, graph=graph, vault_path=vault)

    await indexer.walk()
    await indexer.walk()  # re-index same file

    result = await retriever.search("Stable content", tier_limit=1)
    # Same content_hash → deduped → exactly 1 observation
    assert len(result.obs_ids) == 1


@pytest.mark.asyncio
async def test_delete_removes_from_search(
    db: DatabaseManager, retriever: HybridRetriever
) -> None:
    """Deleted observations no longer appear in search results."""
    cap = ObservationCapture(db)
    oid = await cap.record("ephemeral data point xyz999")
    assert oid > 0

    await db.delete(oid)
    result = await retriever.search("ephemeral data point xyz999", tier_limit=1)
    assert oid not in result.obs_ids
