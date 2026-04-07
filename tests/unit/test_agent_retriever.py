"""Unit tests for AgentRetriever — agent caching and graceful fallback."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from supermem.retrieval.agent import AgentRetriever
from supermem.storage.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "agent_ret_test.db")
    await d.init()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_agent_cached_across_calls(tmp_path: Path) -> None:
    """Agent is only instantiated once — second call reuses self._agent."""
    retriever = AgentRetriever(memory_path=str(tmp_path))
    assert retriever._agent is None

    fake_result = MagicMock()
    fake_result.reply = "cached reply"

    mock_agent = MagicMock()
    mock_agent.chat.return_value = fake_result

    with patch("agent.Agent", return_value=mock_agent) as MockAgent:
        await retriever.search("first query")
        assert MockAgent.call_count == 1

        await retriever.search("second query")
        # Agent class should not be instantiated again
        assert MockAgent.call_count == 1

    assert retriever._agent is mock_agent


@pytest.mark.asyncio
async def test_agent_reply_written_as_observation(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """A non-empty agent reply is persisted as an observation."""
    retriever = AgentRetriever(memory_path=str(tmp_path), db=db)

    fake_result = MagicMock()
    fake_result.reply = "The answer is 42."

    mock_agent = MagicMock()
    mock_agent.chat.return_value = fake_result

    with patch("agent.Agent", return_value=mock_agent):
        result = await retriever.search("what is the answer?")

    assert result.obs_ids  # should contain the persisted obs_id
    obs = await db.get_observations(result.obs_ids)
    assert any("42" in o["content"] for o in obs)


@pytest.mark.asyncio
async def test_agent_empty_reply_returns_no_ids(tmp_path: Path) -> None:
    """Empty reply → no obs_id returned."""
    retriever = AgentRetriever(memory_path=str(tmp_path))

    fake_result = MagicMock()
    fake_result.reply = ""

    mock_agent = MagicMock()
    mock_agent.chat.return_value = fake_result

    with patch("agent.Agent", return_value=mock_agent):
        result = await retriever.search("empty?")

    assert result.obs_ids == []


@pytest.mark.asyncio
async def test_agent_exception_returns_empty_result(tmp_path: Path) -> None:
    """If Agent.chat raises, search() returns an empty RetrievalResult (no crash)."""
    retriever = AgentRetriever(memory_path=str(tmp_path))

    mock_agent = MagicMock()
    mock_agent.chat.side_effect = RuntimeError("model server down")

    with patch("agent.Agent", return_value=mock_agent):
        result = await retriever.search("will fail")

    assert result.obs_ids == []
    assert result.source_tier == 4
