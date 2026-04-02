"""Unit tests for MemoryCompressor."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from recall.capture.compressor import MemoryCompressor
from recall.storage.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "compress_test.db")
    await d.init()
    yield d
    await d.close()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.chat_completion = AsyncMock(return_value="Compressed summary of recent observations.")
    return client


@pytest.mark.asyncio
async def test_no_trigger_below_threshold(db: DatabaseManager, mock_client: MagicMock) -> None:
    """maybe_compress should not trigger LLM before threshold is reached."""
    compressor = MemoryCompressor(db, model_client=mock_client, compress_every=10)
    sid = await db.create_session()
    for _ in range(9):
        await compressor.maybe_compress(sid)
    mock_client.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_triggers_at_threshold(db: DatabaseManager, mock_client: MagicMock) -> None:
    """maybe_compress calls LLM exactly when write_count % compress_every == 0."""
    compressor = MemoryCompressor(db, model_client=mock_client, compress_every=5)
    sid = await db.create_session()
    # Write 5 real observations so _compress_session has content
    for i in range(5):
        await db.write_observation(f"observation {i}", session_id=sid)
    for _ in range(5):
        await compressor.maybe_compress(sid)
    mock_client.chat_completion.assert_called_once()


@pytest.mark.asyncio
async def test_no_client_skips_silently(db: DatabaseManager) -> None:
    """With no model_client set, compression is silently skipped."""
    compressor = MemoryCompressor(db, model_client=None, compress_every=1)
    sid = await db.create_session()
    await compressor.maybe_compress(sid)  # should not raise


@pytest.mark.asyncio
async def test_set_model_client_injects_later(db: DatabaseManager, mock_client: MagicMock) -> None:
    compressor = MemoryCompressor(db, compress_every=5)
    compressor.set_model_client(mock_client)
    assert compressor._model_client is mock_client


@pytest.mark.asyncio
async def test_too_few_observations_skips(db: DatabaseManager, mock_client: MagicMock) -> None:
    """Compression requires at least 5 observations; fewer → no LLM call."""
    compressor = MemoryCompressor(db, model_client=mock_client, compress_every=1)
    sid = await db.create_session()
    # Only 2 observations — below the 5-obs minimum
    for i in range(2):
        await db.write_observation(f"short obs {i}", session_id=sid)
    await compressor.maybe_compress(sid)
    mock_client.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_llm_failure_does_not_raise(db: DatabaseManager) -> None:
    """Compression failure is caught; no exception propagates."""
    failing_client = MagicMock()
    failing_client.chat_completion = AsyncMock(side_effect=Exception("LLM down"))
    compressor = MemoryCompressor(db, model_client=failing_client, compress_every=1)
    sid = await db.create_session()
    for i in range(5):
        await db.write_observation(f"content {i}", session_id=sid)
    await compressor.maybe_compress(sid)  # should not raise
