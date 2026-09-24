"""Unit tests for MemoryCompressor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from supermem.capture.compressor import MemoryCompressor
from supermem.storage.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "compress_test.db")
    await d.init()
    yield d
    await d.close()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.chat_completion = AsyncMock(
        return_value="Compressed summary of recent observations."
    )
    return client


@pytest.mark.asyncio
async def test_no_trigger_below_threshold(
    db: DatabaseManager, mock_client: MagicMock
) -> None:
    """maybe_compress should not trigger LLM before threshold is reached."""
    compressor = MemoryCompressor(db, model_client=mock_client, compress_every=10)
    sid = await db.create_session()
    for _ in range(9):
        await compressor.maybe_compress(sid)
    mock_client.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_triggers_at_threshold(
    db: DatabaseManager, mock_client: MagicMock
) -> None:
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
async def test_set_model_client_injects_later(
    db: DatabaseManager, mock_client: MagicMock
) -> None:
    compressor = MemoryCompressor(db, compress_every=5)
    compressor.set_model_client(mock_client)
    assert compressor._model_client is mock_client


@pytest.mark.asyncio
async def test_too_few_observations_skips(
    db: DatabaseManager, mock_client: MagicMock
) -> None:
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


# ── compress_to_budget: retrievability-gated compression ─────────────────────

_OBS_TEXTS = [
    "zephyr pipeline deploys to the east region nightly",
    "kubernetes rollout paused following a failed canary",
    "monitoring dashboards reveal elevated latency spikes",
]

_COVERING_SUMMARY = (
    "zephyr pipeline deploys nightly to the east region; "
    "kubernetes rollout paused after a failed canary; "
    "monitoring dashboards reveal elevated latency spikes"
)


async def _seed_obs(db: DatabaseManager, sid: int) -> list[dict]:
    obs_list = []
    for text in _OBS_TEXTS:
        oid = await db.write_observation(text, session_id=sid)
        obs_list.append({"id": oid, "content": text, "session_id": sid})
    return obs_list


@pytest.mark.asyncio
async def test_compress_to_budget_archives_sources_only_after_gate(
    db: DatabaseManager,
) -> None:
    """Passing budget + coverage + retrievability → sources archived."""
    sid = await db.create_session()
    obs_list = await _seed_obs(db, sid)
    ids = [o["id"] for o in obs_list]
    client = MagicMock()
    client.chat_completion = AsyncMock(return_value=_COVERING_SUMMARY)
    compressor = MemoryCompressor(db, model_client=client)

    receipt = await compressor.compress_to_budget(obs_list, session_id=sid)

    assert receipt["compressed"] is True
    assert receipt["archived"] == len(ids)
    assert receipt["coverage"] >= 0.6
    # Sources left the active set and the FTS index.
    assert await db.active_obs_ids(ids) == []
    assert await db.fts_search("kubernetes", limit=5) != []
    canary_hits = await db.fts_search("canary", limit=5)
    assert all(i not in canary_hits for i in ids)
    # The summary observation is itself retrievable.
    sid_summary = receipt["summary_obs_id"]
    assert sid_summary in await db.active_obs_ids([sid_summary])
    assert sid_summary in await db.fts_search("zephyr", limit=5)


@pytest.mark.asyncio
async def test_compress_to_budget_rejects_low_coverage(
    db: DatabaseManager,
) -> None:
    """A lossy summary must NOT archive its sources."""
    sid = await db.create_session()
    obs_list = await _seed_obs(db, sid)
    ids = [o["id"] for o in obs_list]
    client = MagicMock()
    client.chat_completion = AsyncMock(
        return_value="Something happened in some systems somewhere."
    )
    compressor = MemoryCompressor(db, model_client=client)

    receipt = await compressor.compress_to_budget(obs_list, session_id=sid)

    assert receipt["compressed"] is False
    assert receipt["reason"] == "coverage_below_gate"
    assert receipt["archived"] == 0
    # Sources remain fully retrievable.
    assert sorted(await db.active_obs_ids(ids)) == ids


@pytest.mark.asyncio
async def test_compress_to_budget_rejects_over_budget(
    db: DatabaseManager,
) -> None:
    """A summary exceeding the char budget is rejected, not truncated."""
    sid = await db.create_session()
    obs_list = await _seed_obs(db, sid)
    client = MagicMock()
    client.chat_completion = AsyncMock(return_value="x " * 300)
    compressor = MemoryCompressor(db, model_client=client)

    receipt = await compressor.compress_to_budget(
        obs_list, session_id=sid, budget_chars=200
    )

    assert receipt["compressed"] is False
    assert receipt["reason"] == "over_budget"


@pytest.mark.asyncio
async def test_compress_to_budget_requires_retrievable_summary(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the written summary cannot be retrieved, sources are NOT archived."""
    sid = await db.create_session()
    obs_list = await _seed_obs(db, sid)
    ids = [o["id"] for o in obs_list]
    client = MagicMock()
    client.chat_completion = AsyncMock(return_value=_COVERING_SUMMARY)
    compressor = MemoryCompressor(db, model_client=client)
    monkeypatch.setattr(db, "fts_search", AsyncMock(return_value=[]))

    receipt = await compressor.compress_to_budget(obs_list, session_id=sid)

    assert receipt["compressed"] is False
    assert receipt["reason"] == "summary_not_retrievable"
    assert sorted(await db.active_obs_ids(ids)) == ids


@pytest.mark.asyncio
async def test_compress_to_budget_no_model_client(db: DatabaseManager) -> None:
    sid = await db.create_session()
    obs_list = await _seed_obs(db, sid)
    compressor = MemoryCompressor(db, model_client=None)
    receipt = await compressor.compress_to_budget(obs_list, session_id=sid)
    assert receipt["compressed"] is False
    assert receipt["reason"] == "no_model_client"
