"""Unit tests for DatabaseManager — SQLite FTS5 storage."""

from __future__ import annotations

import time
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
async def test_private_observation_is_stripped_before_persistence(
    db: DatabaseManager,
) -> None:
    oid = await db.write_observation("public-ember <private>private-canary")
    observations = await db.get_observations([oid])
    assert observations[0]["content"] == "public-ember"
    assert await db.fts_search("private-canary") == []


@pytest.mark.asyncio
async def test_stray_private_closer_fails_closed_before_database_persistence(
    db: DatabaseManager,
) -> None:
    oid = await db.write_observation("public-ember </private> private-canary")
    assert (await db.get_observations([oid]))[0]["content"] == "public-ember"
    assert await db.fts_search("private-canary") == []


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


# ── P0 lifecycle: live expiry, supersession, archive ──────────────────────────


@pytest.mark.asyncio
async def test_expired_observation_excluded_from_retrieval(db: DatabaseManager) -> None:
    sid = await db.create_session()
    oid = await db.write_observation("permanent fact", session_id=sid)
    # Simulate a TTL that has already elapsed.
    await db._conn.execute(
        "UPDATE observations SET expires_at = ? WHERE id = ?", (0.0, oid)
    )
    await db._conn.commit()

    # Expired rows must not surface via any active-query path.
    assert await db.fts_search("permanent") == []
    assert await db.active_obs_ids([oid]) == []
    assert await db.get_observations([oid]) == []
    assert await db.get_recent_observations(sid) == []
    assert await db.get_recent_observations_by_age() == []


@pytest.mark.asyncio
async def test_maybe_purge_expired_deletes(db: DatabaseManager) -> None:
    sid = await db.create_session()
    oid = await db.write_observation("gone soon", session_id=sid)
    await db._conn.execute(
        "UPDATE observations SET expires_at = ? WHERE id = ?", (0.0, oid)
    )
    await db._conn.commit()

    count = await db.maybe_purge_expired(throttle_seconds=0)
    assert count >= 1
    obs = await db.get_observations([oid])
    assert obs == []


@pytest.mark.asyncio
async def test_supersede_by_source_marks_prior_active_rows(db: DatabaseManager) -> None:
    uri = "vault/entities/alice.md"
    old = await db.write_observation(
        "Alice works at Acme",
        obs_type="entity_content",
        source_id=uri,
        status="active",
    )
    new = await db.write_observation(
        "Alice now works at Globex",
        obs_type="entity_content",
        source_id=uri,
        status="active",
    )

    superseded = await db.supersede_by_source(uri, exclude_id=new)
    assert superseded == 1

    # Old revision is no longer retrievable; new one still is.
    assert await db.active_obs_ids([old]) == []
    assert await db.active_obs_ids([new]) == [new]
    # FTS must not return the stale revision.
    assert await db.fts_search("Acme") == []
    assert await db.fts_search("Globex") == [new]


@pytest.mark.asyncio
async def test_supersede_by_source_invalidates_summaries(db: DatabaseManager) -> None:
    sid = await db.create_session()
    uri = "vault/entities/alice.md"
    oid = await db.write_observation(
        "Old Alice fact",
        session_id=sid,
        obs_type="entity_content",
        source_id=uri,
        status="active",
    )
    await db.write_summary(sid, "Summary referencing old fact", [oid])

    await db.supersede_by_source(uri)
    async with db._conn.execute(
        "SELECT COUNT(*) FROM summaries WHERE session_id = ?", (sid,)
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 0


@pytest.mark.asyncio
async def test_archive_observations_preserves_text(db: DatabaseManager) -> None:
    sid = await db.create_session()
    oid = await db.write_observation("archivable fact", session_id=sid)
    archived = await db.archive_observations([oid])
    assert archived == 1

    # Excluded from retrieval but the row (text) still exists for audit.
    assert await db.active_obs_ids([oid]) == []
    async with db._conn.execute(
        "SELECT content, status FROM observations WHERE id = ?", (oid,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "archivable fact"
    assert row[1] == "archived"


# ── FTS MATCH sanitization: arbitrary user text must not raise ────────────────


@pytest.mark.asyncio
async def test_fts_search_handles_apostrophes(db: DatabaseManager) -> None:
    sid = await db.create_session()
    oid = await db.write_observation("Alice's office is in Berlin", session_id=sid)
    # Raw apostrophes used to raise "fts5: syntax error" and silently return [].
    assert await db.fts_search("Alice's office") == [oid]


@pytest.mark.asyncio
async def test_fts_search_handles_quotes_and_punctuation(db: DatabaseManager) -> None:
    sid = await db.create_session()
    oid = await db.write_observation(
        'the "quoted" project-alpha kickoff e-mail went out', session_id=sid
    )
    results = await db.fts_search('project-alpha "quoted" e-mail')
    assert oid in results
    assert await db.fts_search("") == []


# ── Temporal validity: effective-time windows ─────────────────────────────────


@pytest.mark.asyncio
async def test_valid_from_in_future_excluded_now(db: DatabaseManager) -> None:
    future = time.time() + 86400
    oid = await db.write_observation("Alice becomes CEO next year", valid_from=future)
    assert await db.fts_search("Alice") == []
    assert await db.active_obs_ids([oid]) == []
    assert await db.get_observations([oid]) == []


@pytest.mark.asyncio
async def test_valid_from_in_past_visible(db: DatabaseManager) -> None:
    past = time.time() - 86400
    oid = await db.write_observation("Alice joined Acme last year", valid_from=past)
    assert oid in await db.fts_search("Acme")
    assert await db.active_obs_ids([oid]) == [oid]


@pytest.mark.asyncio
async def test_expired_valid_until_excluded(db: DatabaseManager) -> None:
    past = time.time() - 86400
    oid = await db.write_observation("Alice lives in Berlin", valid_until=past)
    assert await db.fts_search("Berlin") == []
    assert await db.active_obs_ids([oid]) == []
    assert await db.get_observations([oid]) == []


@pytest.mark.asyncio
async def test_active_window_visible(db: DatabaseManager) -> None:
    oid = await db.write_observation(
        "Alice is on the platform team",
        valid_from=time.time() - 3600,
        valid_until=time.time() + 3600,
    )
    assert oid in await db.fts_search("platform")
    assert await db.get_observations([oid]) != []


@pytest.mark.asyncio
async def test_null_window_rows_unaffected(db: DatabaseManager) -> None:
    sid = await db.create_session()
    oid = await db.write_observation("plain observation with no window", session_id=sid)
    assert oid in await db.fts_search("window")
    assert await db.active_obs_ids([oid]) == [oid]
    assert await db.get_observations([oid])
    assert await db.get_timeline(oid)
    assert any(r["id"] == oid for r in await db.get_recent_observations(sid))
    assert any(r["id"] == oid for r in await db.get_recent_observations_by_age())


@pytest.mark.asyncio
async def test_set_validity_window_round_trip(db: DatabaseManager) -> None:
    start = time.time() - 7200
    end = time.time() + 7200
    oid = await db.write_observation("Alice is visiting Oslo")

    assert await db.set_validity_window(oid, valid_from=start, valid_until=end) is True
    obs = await db.get_observations([oid])
    assert obs and abs(obs[0]["valid_from"] - start) < 1e-6
    assert abs(obs[0]["valid_until"] - end) < 1e-6

    # Closing the window hides the row from retrieval.
    await db.set_validity_window(oid, valid_until=time.time() - 60)
    assert await db.get_observations([oid]) == []

    # Clearing the bounds (None) restores default visibility.
    await db.set_validity_window(oid, valid_from=None, valid_until=None)
    assert oid in await db.fts_search("Oslo")


@pytest.mark.asyncio
async def test_set_validity_window_missing_id_returns_false(
    db: DatabaseManager,
) -> None:
    assert await db.set_validity_window(999999, valid_from=0.0) is False
