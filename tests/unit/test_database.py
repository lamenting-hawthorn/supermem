"""Unit tests for DatabaseManager — SQLite FTS5 storage."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from supermem.storage.database import DatabaseManager


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
    oid = await db.write_observation(
        "hello world", session_id=sid, tier_used=1, latency_ms=5.0
    )
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
    async with db._conn.execute(
        "SELECT summary FROM sessions WHERE id=?", (sid,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "test summary"


@pytest.mark.asyncio
async def test_upsert_entity(db: DatabaseManager) -> None:
    await db.upsert_entity("alice", "/vault/entities/alice.md", wikilink_count=3)
    await db.upsert_entity(
        "alice", "/vault/entities/alice.md", wikilink_count=5
    )  # upsert
    async with db._conn.execute(
        "SELECT wikilink_count FROM entity_metadata WHERE name='alice'"
    ) as cur:
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


# ── get_entity_last_indexed ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_entity_last_indexed_missing(db: DatabaseManager) -> None:
    result = await db.get_entity_last_indexed("does-not-exist")
    assert result is None


@pytest.mark.asyncio
async def test_get_entity_last_indexed_after_upsert(db: DatabaseManager) -> None:
    import time

    before = time.time()
    await db.upsert_entity("my/note", "/vault/my/note.md", wikilink_count=2)
    after = time.time()

    ts = await db.get_entity_last_indexed("my/note")
    assert ts is not None
    assert before <= ts <= after


# ── entities_for_obs_ids (FTS5 path) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_entities_for_obs_ids_finds_mention(db: DatabaseManager) -> None:
    await db.upsert_entity("Alice", "/vault/Alice.md")
    obs_id = await db.write_observation("Alice works at Acme Corp.")
    names = await db.entities_for_obs_ids([obs_id])
    assert "Alice" in names


@pytest.mark.asyncio
async def test_entities_for_obs_ids_empty_input(db: DatabaseManager) -> None:
    assert await db.entities_for_obs_ids([]) == []


@pytest.mark.asyncio
async def test_entities_for_obs_ids_no_match(db: DatabaseManager) -> None:
    await db.upsert_entity("Zzz", "/vault/Zzz.md")
    obs_id = await db.write_observation("Content that does not mention that entity.")
    names = await db.entities_for_obs_ids([obs_id])
    assert "Zzz" not in names


# ── obs_ids_for_entities (FTS5 path) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_obs_ids_for_entities_finds_match(db: DatabaseManager) -> None:
    obs_id = await db.write_observation("Bob joined the team last quarter.")
    ids = await db.obs_ids_for_entities(["Bob"])
    assert obs_id in ids


@pytest.mark.asyncio
async def test_obs_ids_for_entities_empty_input(db: DatabaseManager) -> None:
    assert await db.obs_ids_for_entities([]) == []


@pytest.mark.asyncio
async def test_obs_ids_for_entities_dedup(db: DatabaseManager) -> None:
    obs_id = await db.write_observation("Carol and Carol again.")
    # Searching for the same entity twice should not duplicate the obs_id
    ids = await db.obs_ids_for_entities(["Carol", "Carol"])
    assert ids.count(obs_id) == 1


@pytest.mark.asyncio
async def test_get_recent_observations_by_age(db: DatabaseManager) -> None:
    oid = await db.write_observation("TODO: follow up with Alice")
    rows = await db.get_recent_observations_by_age(days=1, limit=10)
    assert any(row["id"] == oid for row in rows)


@pytest.mark.asyncio
async def test_write_observation_metadata_fields(db: DatabaseManager) -> None:
    oid = await db.write_observation(
        "Alice prefers tea",
        source_id="chatgpt-export",
        source_span="conversation-1#message-2",
        observed_at=123.0,
        valid_from=120.0,
        confidence=0.7,
        trust_level="user",
        sensitivity="normal",
    )
    obs = await db.get_observations([oid])
    assert obs[0]["source_id"] == "chatgpt-export"
    assert obs[0]["source_span"] == "conversation-1#message-2"
    assert obs[0]["observed_at"] == 123.0
    assert obs[0]["valid_from"] == 120.0
    assert obs[0]["confidence"] == 0.7
    assert obs[0]["trust_level"] == "user"
    assert obs[0]["status"] == "active"


@pytest.mark.asyncio
async def test_retract_observation_removes_from_retrieval(db: DatabaseManager) -> None:
    oid = await db.write_observation("Secret launch codename is Bluebird")
    assert oid in await db.fts_search("Bluebird")
    assert await db.retract_observation(oid, reason="stale fact") is True
    assert oid not in await db.fts_search("Bluebird")
    assert await db.get_observations([oid]) == []


@pytest.mark.asyncio
async def test_active_obs_ids_preserves_order_and_filters_retracted(
    db: DatabaseManager,
) -> None:
    first = await db.write_observation("first active memory")
    second = await db.write_observation("second stale memory")
    third = await db.write_observation("third active memory")
    await db.retract_observation(second)

    assert await db.active_obs_ids([third, second, first]) == [third, first]


@pytest.mark.asyncio
async def test_timeline_filters_retracted_neighbors(db: DatabaseManager) -> None:
    sid = await db.create_session()
    before = await db.write_observation("before active", session_id=sid)
    stale = await db.write_observation("middle stale", session_id=sid)
    after = await db.write_observation("after active", session_id=sid)
    await db.retract_observation(stale, reason="contains sensitive token")

    timeline = await db.get_timeline(before, window=5)
    ids = [row["id"] for row in timeline]
    assert stale not in ids
    assert after in ids


@pytest.mark.asyncio
async def test_recent_observations_filters_retracted_rows(db: DatabaseManager) -> None:
    sid = await db.create_session()
    active = await db.write_observation("active recent", session_id=sid)
    stale = await db.write_observation("stale recent", session_id=sid)
    await db.retract_observation(stale)

    rows = await db.get_recent_observations(sid, limit=10)
    ids = [row["id"] for row in rows]
    assert active in ids
    assert stale not in ids


@pytest.mark.asyncio
async def test_retract_reason_is_not_indexed(db: DatabaseManager) -> None:
    oid = await db.write_observation("temporary sensitive record")
    await db.retract_observation(oid, reason="SSN 123-45-6789")

    assert await db.fts_search("123") == []
    async with db._conn.execute(
        "SELECT reason FROM retraction_audit WHERE obs_id = ?", (oid,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "SSN 123-45-6789"


@pytest.mark.asyncio
async def test_rewrite_after_retraction_creates_active_row(db: DatabaseManager) -> None:
    sid = await db.create_session()
    stale = await db.write_observation("same fact", session_id=sid)
    await db.retract_observation(stale)

    fresh = await db.write_observation("same fact", session_id=sid)

    assert fresh != stale
    assert fresh in await db.fts_search("same")


@pytest.mark.asyncio
async def test_retract_observation_invalidates_session_summaries(
    db: DatabaseManager,
) -> None:
    sid = await db.create_session()
    oid = await db.write_observation("Sensitive launch fact", session_id=sid)
    await db.close_session(sid, "Summary leaks Sensitive launch fact")
    await db.write_summary(sid, "Compressed leak Sensitive launch fact", [oid])

    assert await db.retract_observation(oid, reason="forget sensitive fact") is True

    async with db._conn.execute(
        "SELECT summary FROM sessions WHERE id = ?", (sid,)
    ) as cur:
        session = await cur.fetchone()
    assert session[0] is None
    async with db._conn.execute(
        "SELECT COUNT(*) FROM summaries WHERE session_id = ?", (sid,)
    ) as cur:
        summary_count = (await cur.fetchone())[0]
    assert summary_count == 0
