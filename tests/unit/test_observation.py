"""Unit tests for ObservationCapture."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from recall.capture.observation import ObservationCapture
from recall.storage.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "obs_test.db")
    await d.init()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_record_basic(db: DatabaseManager) -> None:
    cap = ObservationCapture(db)
    oid = await cap.record("test content", tier_used=1, latency_ms=10.0)
    assert oid > 0
    obs = await db.get_observations([oid])
    assert obs[0]["content"] == "test content"
    assert obs[0]["tier_used"] == 1


@pytest.mark.asyncio
async def test_record_strips_private(db: DatabaseManager) -> None:
    cap = ObservationCapture(db)
    oid = await cap.record("public <private>secret123</private> content")
    obs = await db.get_observations([oid])
    assert "secret123" not in obs[0]["content"]
    assert "public" in obs[0]["content"]


@pytest.mark.asyncio
async def test_record_all_private_skipped(db: DatabaseManager) -> None:
    cap = ObservationCapture(db)
    oid = await cap.record("<private>everything is secret</private>")
    assert oid == -1


@pytest.mark.asyncio
async def test_record_with_session(db: DatabaseManager) -> None:
    sid = await db.create_session()
    cap = ObservationCapture(db)
    oid = await cap.record("session content", session_id=sid)
    obs = await db.get_observations([oid])
    assert obs[0]["session_id"] == sid


@pytest.mark.asyncio
async def test_record_tool_name(db: DatabaseManager) -> None:
    cap = ObservationCapture(db)
    oid = await cap.record("tool call result", tool_name="use_memory_agent")
    obs = await db.get_observations([oid])
    assert obs[0]["tool_name"] == "use_memory_agent"
