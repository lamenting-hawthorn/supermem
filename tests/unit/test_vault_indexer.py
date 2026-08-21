"""Unit tests for VaultIndexer — mtime dirty-check and indexing behaviour."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import pytest_asyncio

from supermem.indexer.vault import VaultIndexer
from supermem.storage.database import DatabaseManager
from supermem.storage.graph import KuzuGraphManager


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "vault_test.db")
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
async def indexer(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path
) -> VaultIndexer:
    return VaultIndexer(db=db, graph=graph, vault_path=vault)


@pytest.mark.asyncio
async def test_index_file_creates_entity(
    indexer: VaultIndexer, db: DatabaseManager, vault: Path
) -> None:
    note = vault / "Alice.md"
    note.write_text("# Alice\n\nAlice works at Acme.")

    await indexer.index_file(note)

    ts = await db.get_entity_last_indexed("Alice")
    assert ts is not None


@pytest.mark.asyncio
async def test_index_file_stray_private_closer_fails_closed(
    indexer: VaultIndexer, db: DatabaseManager, vault: Path
) -> None:
    note = vault / "Private.md"
    note.write_text("public-slate </private> private-canary")
    await indexer.index_file(note)
    assert await db.fts_search("canary") == []
    assert await db.fts_search("public")


@pytest.mark.asyncio
async def test_index_file_skips_unchanged(
    indexer: VaultIndexer, db: DatabaseManager, vault: Path
) -> None:
    note = vault / "Bob.md"
    note.write_text("# Bob")
    await indexer.index_file(note)

    # Record initial obs count
    stats = await db.get_stats()
    obs_before = stats["obs_count"]

    # Re-index without touching the file — mtime hasn't changed, should skip
    await indexer.index_file(note)

    stats = await db.get_stats()
    assert stats["obs_count"] == obs_before


@pytest.mark.asyncio
async def test_index_file_reindexes_when_content_changes(
    indexer: VaultIndexer, db: DatabaseManager, vault: Path
) -> None:
    note = vault / "Carol.md"
    note.write_text("# Carol v1")
    await indexer.index_file(note)

    stats = await db.get_stats()
    obs_before = stats["obs_count"]

    # Touch the file to advance mtime, then update content
    time.sleep(0.01)
    note.write_text("# Carol v2 — updated content")
    # Force mtime to be strictly after last_indexed by setting it explicitly
    new_mtime = time.time() + 1
    import os

    os.utime(note, (new_mtime, new_mtime))

    await indexer.index_file(note)

    stats = await db.get_stats()
    assert stats["obs_count"] > obs_before


@pytest.mark.asyncio
async def test_walk_indexes_all_files(
    indexer: VaultIndexer, db: DatabaseManager, vault: Path
) -> None:
    (vault / "A.md").write_text("Note A")
    (vault / "B.md").write_text("Note B")
    (vault / "sub").mkdir()
    (vault / "sub" / "C.md").write_text("Note C in sub")

    count = await indexer.walk()
    assert count == 3


# ── Stale-revision supersession (SF-4) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_reindex_supersedes_old_entity_content(
    indexer: VaultIndexer, db: DatabaseManager, vault: Path
) -> None:
    note = vault / "Dave.md"
    note.write_text("# Dave v1 works at Acme")
    await indexer.index_file(note)

    old_ids = await db.fts_search("Acme")
    assert len(old_ids) == 1
    old_id = old_ids[0]

    # Change content + advance mtime strictly past last_indexed.
    time.sleep(0.01)
    note.write_text("# Dave v2 now at Globex")
    new_mtime = time.time() + 1
    os.utime(note, (new_mtime, new_mtime))

    await indexer.index_file(note)

    new_ids = await db.fts_search("Globex")
    assert len(new_ids) == 1
    new_id = new_ids[0]
    assert new_id != old_id

    # Only the newest revision is active and searchable.
    assert await db.fts_search("Acme") == []
    assert await db.active_obs_ids([old_id]) == []
    assert await db.active_obs_ids([new_id]) == [new_id]


# ── Deletion handling (walk reconciliation) ──────────────────────────────────


@pytest.mark.asyncio
async def test_walk_reconciles_deleted_file(
    indexer: VaultIndexer, db: DatabaseManager, vault: Path
) -> None:
    note = vault / "Eve.md"
    note.write_text("# Eve at Acme Corp")
    await indexer.index_file(note)
    assert await db.get_entity_last_indexed("Eve") is not None

    note.unlink()
    await indexer.walk()

    assert await db.get_entity_last_indexed("Eve") is None
    assert await db.fts_search("Acme") == []


@pytest.mark.asyncio
async def test_on_deleted_supersedes_and_removes_metadata(
    indexer: VaultIndexer, db: DatabaseManager, vault: Path
) -> None:
    note = vault / "Mallory.md"
    note.write_text("# Mallory secret data")
    await indexer.index_file(note)

    await indexer.on_deleted(note)

    assert await db.get_entity_last_indexed("Mallory") is None
    assert await db.fts_search("secret") == []


# ── Vector ingestion ─────────────────────────────────────────────────────────


class _FakeVector:
    """In-memory stand-in for ChromaManager in indexer tests."""

    def __init__(self) -> None:
        self.available = True
        self.upsert_calls: list[dict] = []
        self.deleted_sources: list[str] = []

    async def upsert_chunks(
        self,
        chunks: list[str],
        *,
        obs_id: int | None = None,
        source_uri: str | None = None,
    ) -> None:
        self.upsert_calls.append(
            {"chunks": chunks, "obs_id": obs_id, "source_uri": source_uri}
        )

    async def delete_by_source(self, source_uri: str) -> None:
        self.deleted_sources.append(source_uri)


@pytest.mark.asyncio
async def test_index_file_ingests_vectors_when_available(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path
) -> None:
    fake = _FakeVector()
    indexer = VaultIndexer(db=db, graph=graph, vector=fake, vault_path=vault)

    note = vault / "Frank.md"
    note.write_text("# Frank\n\n" + ("chunkable content " * 500))
    await indexer.index_file(note)

    assert len(fake.upsert_calls) == 1
    call = fake.upsert_calls[0]
    assert call["source_uri"] == "Frank"
    assert call["obs_id"] is not None
    assert len(call["chunks"]) > 1  # long file → multiple chunks


@pytest.mark.asyncio
async def test_index_file_no_vector_does_not_raise(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path
) -> None:
    indexer = VaultIndexer(db=db, graph=graph, vector=None, vault_path=vault)
    note = vault / "Grace.md"
    note.write_text("# Grace")
    await indexer.index_file(note)  # must not raise


@pytest.mark.asyncio
async def test_on_deleted_cleans_vectors(
    db: DatabaseManager, graph: KuzuGraphManager, vault: Path
) -> None:
    fake = _FakeVector()
    indexer = VaultIndexer(db=db, graph=graph, vector=fake, vault_path=vault)

    note = vault / "Heidi.md"
    note.write_text("# Heidi content")
    await indexer.index_file(note)
    await indexer.on_deleted(note)

    assert "Heidi" in fake.deleted_sources
