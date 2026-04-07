"""Unit tests for VaultIndexer — mtime dirty-check and indexing behaviour."""

from __future__ import annotations

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
