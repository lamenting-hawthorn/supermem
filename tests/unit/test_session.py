"""Unit tests for SessionManager — start/end/summary flow."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from recall.capture.session import SessionManager
from recall.storage.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "session_test.db")
    await d.init()
    yield d
    await d.close()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.chat_completion = AsyncMock(return_value="Session summary text.")
    return client


@pytest.mark.asyncio
async def test_start_returns_session_id(db: DatabaseManager) -> None:
    mgr = SessionManager(db)
    sid = await mgr.start()
    assert isinstance(sid, int)
    assert sid > 0


@pytest.mark.asyncio
async def test_start_creates_unique_sessions(db: DatabaseManager) -> None:
    mgr = SessionManager(db)
    sid1 = await mgr.start()
    sid2 = await mgr.start()
    assert sid1 != sid2


@pytest.mark.asyncio
async def test_start_with_correlation_id(db: DatabaseManager) -> None:
    mgr = SessionManager(db)
    sid = await mgr.start(correlation_id="req-abc-123")
    assert sid > 0


@pytest.mark.asyncio
async def test_end_writes_summary(db: DatabaseManager, mock_client: MagicMock) -> None:
    mgr = SessionManager(db)
    sid = await mgr.start()
    # Write some observations so the compressor has content
    for i in range(3):
        await db.write_observation(f"observation {i}", session_id=sid)
    await mgr.end(sid, mock_client)
    # Summary should be stored
    async with db._conn.execute("SELECT summary FROM sessions WHERE id=?", (sid,)) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_end_no_observations_skips_llm(db: DatabaseManager, mock_client: MagicMock) -> None:
    """When session has no observations, no LLM call should be made."""
    mgr = SessionManager(db)
    sid = await mgr.start()
    await mgr.end(sid, mock_client)
    mock_client.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_end_handles_llm_failure_gracefully(db: DatabaseManager) -> None:
    failing_client = MagicMock()
    failing_client.chat_completion = AsyncMock(side_effect=Exception("LLM unavailable"))
    mgr = SessionManager(db)
    sid = await mgr.start()
    await db.write_observation("some content", session_id=sid)
    # Should not raise even if LLM fails
    await mgr.end(sid, failing_client)
