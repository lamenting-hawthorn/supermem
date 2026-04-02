"""Unit tests for DatabaseManager — SQLite FTS5 storage."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from recall.storage.database import DatabaseManager


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest_asyncio.fixture
async def db(db_path: Path) -> DatabaseManager:
    d = DatabaseManager(db_path)
    await d.init()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_health(db: DatabaseManager) -> None:
    assert await db.health() is True


@pytest.mark.asyncio
async def test_create_session(db: DatabaseManager) -> None:
    sid = await db.create_session(correlation_id="test-cid")
    assert isinstance(sid, int)
    assert sid > 0


@pytest.mark.asyncio
async def test_write_and_read_observation(db: DatabaseManager) -> None:
    sid = await db.create_session()
    oid = await db.write_observation("hello world", session_id=sid, tier_used=1, latency_ms=5.0)
    assert isinstance(oid, int)
    obs = await db.get_observations([oid])
    assert len(obs) == 1
    assert obs[0]["content"] == "hello world"
    assert obs[0]["tier_used"] == 1


@pytest.mark.asyncio
async def test_dedup_by_hash(db: DatabaseManager) -> None:
    sid = await db.create_session()
    oid1 = await db.write_observation("duplicate content", session_id=sid)
    oid2 = await db.write_observation("duplicate content", session_id=sid)
    assert oid1 == oid2  # same session, same content → same ID


@pytest.mark.asyncio
async def test_fts_search_finds_match(db: DatabaseManager) -> None:
    await db.write_observation("alice works at acme corporation")
    await db.write_observation("bob is a software engineer")
    ids = await db.fts_search("alice")
    assert len(ids) >= 1


@pytest.mark.asyncio
async def test_fts_search_no_match(db: DatabaseManager) -> None:
    await db.write_observation("completely unrelated content xyz987")
    ids = await db.fts_search("definitely not present abcxyz")
    assert ids == []


@pytest.mark.asyncio
async def test_fts_search_malformed_query_degrades(db: DatabaseManager) -> None:
    # FTS5 treats some chars specially; ensure we don't raise
    ids = await db.fts_search('AND OR "unclosed')
    assert isinstance(ids, list)


@pytest.mark.asyncio
async def test_get_timeline(db: DatabaseManager) -> None:
    sid = await db.create_session()
    ids = []
    for i in range(5):
        oid = await db.write_observation(f"observation {i}", session_id=sid)
        ids.append(oid)
    timeline = await db.get_timeline(ids[2], window=2)
    assert len(timeline) > 0
    contents = [t["content"] for t in timeline]
    assert "observation 2" in contents


@pytest.mark.asyncio
async def test_get_timeline_missing_obs(db: DatabaseManager) -> None:
    result = await db.get_timeline(99999, window=5)
    assert result == []


@pytest.mark.asyncio
async def test_close_session_with_summary(db: DatabaseManager) -> None:
    sid = await db.create_session()
    await db.close_session(sid, "test summary")
    # Verify summary was stored by querying directly
    async with db._conn.execute("SELECT summary FROM sessions WHERE id=?", (sid,)) as cur:
        row = await cur.fetchone()
    assert row[0] == "test summary"


@pytest.mark.asyncio
async def test_upsert_entity(db: DatabaseManager) -> None:
    await db.upsert_entity("alice", "/vault/entities/alice.md", wikilink_count=3)
    await db.upsert_entity("alice", "/vault/entities/alice.md", wikilink_count=5)  # upsert
    async with db._conn.execute("SELECT wikilink_count FROM entity_metadata WHERE name='alice'") as cur:
        row = await cur.fetchone()
    assert row[0] == 5  # updated


@pytest.mark.asyncio
async def test_get_stats(db: DatabaseManager) -> None:
    await db.write_observation("stat test content")
    stats = await db.get_stats()
    assert stats["obs_count"] >= 1
    assert "entity_count" in stats
    assert "session_count" in stats
    assert "db_size_mb" in stats


@pytest.mark.asyncio
async def test_write_summary(db: DatabaseManager) -> None:
    sid = await db.create_session()
    row_id = await db.write_summary(sid, "compressed summary", [1, 2, 3])
    assert row_id > 0


@pytest.mark.asyncio
async def test_delete_observation(db: DatabaseManager) -> None:
    oid = await db.write_observation("to be deleted")
    deleted = await db.delete(oid)
    assert deleted is True
    obs = await db.get_observations([oid])
    assert obs == []


@pytest.mark.asyncio
async def test_delete_nonexistent(db: DatabaseManager) -> None:
    deleted = await db.delete(999999)
    assert deleted is False


@pytest.mark.asyncio
async def test_get_observations_empty_ids(db: DatabaseManager) -> None:
    result = await db.get_observations([])
    assert result == []
