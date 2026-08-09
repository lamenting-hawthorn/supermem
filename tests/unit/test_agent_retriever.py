"""Unit tests for the unavailable Tier 4 compatibility sentinel."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from supermem.retrieval.agent import AgentRetriever
from supermem.storage.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    database = DatabaseManager(tmp_path / "agent_ret_test.db")
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_tier_four_is_unavailable_and_never_constructs_an_agent(
    tmp_path: Path,
) -> None:
    retriever = AgentRetriever(memory_path=str(tmp_path))

    with patch("agent.Agent") as agent_class:
        result = await retriever.search("lifecycle-canary")

    assert retriever.available is False
    assert result.obs_ids == []
    assert result.source_tier == 4
    assert "unavailable" in result.metadata["unavailable_reason"].lower()
    agent_class.assert_not_called()


@pytest.mark.asyncio
async def test_tier_four_does_not_persist_an_agent_reply(
    tmp_path: Path, db: DatabaseManager
) -> None:
    retriever = AgentRetriever(memory_path=str(tmp_path), db=db)

    with patch("agent.Agent") as agent_class:
        result = await retriever.search("never-persist-this-agent-reply")

    assert result.obs_ids == []
    agent_class.assert_not_called()
    async with db._conn.execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) FROM observations WHERE type = 'agent_reply'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0
